#!/usr/bin/env python3
"""install.py -- flash thingino on a Showmo WinEye Pro (PRJ007, T32NQ, GC8602)
that still runs its vendor firmware, using only the 3.3 V serial console.

What it does, in order (about 1 h, mostly serial transfers at 115200 baud):
  1. stops the vendor u-boot (Ctrl-B at power-on), reads its environment
     (MAC address, sensor) and refuses anything that is not a GC8602/T32NQ;
  2. boots the vendor kernel with a bare shell (no vendor application);
  3. backs up the whole 16 MiB flash to this PC and checks its md5;
  4. copies the sensor calibration files (IQ) from the vendor firmware;
  5. sends the thingino image, writes it partition by partition (u-boot
     first) and checks every partition by md5;
  6. reboots into thingino, sets the root password, restores the vendor
     MAC address, installs the IQ files, and optionally configures Wi-Fi
     and formats the eMMC;
  7. reboots and prints the camera address.

If a step after the flash fails, rerun with --post-only: it only does 6-7.

Requires: python3, pyserial (pip install pyserial).
"""
import argparse
import base64
import getpass
import hashlib
import io
import os
import re
import subprocess
import sys
import time
import zipfile

try:
    import serial
except ImportError:
    sys.exit("pyserial is missing: pip install pyserial")

BAUD = 115200
FLASH_SIZE = 16 * 1024 * 1024
IQ_FILES = ("gc8602-PRJ007-day.bin", "gc8602-PRJ007-night.bin", "gc8602-PRJ007-cust.bin")
# Vendor partitions (offset, size): the image is written in these pieces,
# u-boot first and the running vendor rootfs (mtd2) last.
VENDOR_MTD = {0: (0x000000, 0x060000), 1: (0x060000, 0x210000), 2: (0x270000, 0x490000),
              3: (0x700000, 0x820000), 4: (0xF20000, 0x0A0000), 5: (0xFC0000, 0x040000)}
WRITE_ORDER = (0, 1, 3, 4, 5, 2)
VENDOR_MTDPARTS = "sfc0_nor:384k(boot),2112k(kernel),4672k(root),8320k(system),640k(config),256k(log),16M@0(all)"
UBOOT_SHELL = [
    "mw.l 0x10002004 0",
    "setenv bootargs console=ttyS1,115200n8 mem=95M@0x0 rmem=33M@0x5F00000 init=/bin/sh "
    "rootfstype=squashfs root=/dev/mtdblock2 ro mtdparts=" + VENDOR_MTDPARTS,
    "sf probe 0",
    "sf read 0x80600000 0x60000 0x210000",
    "bootm 0x80600000",
]
VENDOR_LIBS = ("ld-uClibc-0.9.33.2.so", "libm-0.9.33.2.so", "libuClibc-0.9.33.2.so")


def say(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def md5(data):
    return hashlib.md5(data).hexdigest()


class Console:
    """Line-oriented access to a shell (or u-boot) on the serial port."""

    def __init__(self, port, log):
        self.s = serial.Serial(port, BAUD, timeout=0.05)
        self.buf = b""
        self.n = 0
        self.log = log

    def _read(self):
        data = self.s.read(4096)
        if data:
            self.log.write(data)
            self.buf += data
        return data

    def expect(self, pattern, timeout, poke=None):
        """Waits for a regex in the output; returns the match and drops the
        buffer up to it. poke: bytes sent every 50 ms while waiting."""
        rx = re.compile(pattern)
        end = time.time() + timeout
        while time.time() < end:
            m = rx.search(self.buf)
            if m:
                self.buf = self.buf[m.end():]
                return m
            if poke:
                self.s.write(poke)
            self._read()
        raise TimeoutError(f"waited {timeout} s for {pattern!r}; last output: {self.buf[-300:]!r}")

    def send(self, line):
        self.s.write(line.encode() + b"\n")

    def run(self, cmd, timeout=60, check=True):
        """Runs a shell command, returns (rc, output). The output is taken
        between a start and an end marker printed by the shell itself, so the
        tty echo of the command (wrapped at 80 columns) is never parsed."""
        self.n += 1
        n = self.n
        self.buf = b""
        self.send(f'echo "@@S{n}@@"; {cmd}; echo "@@{n}@@$?"')
        m = self.expect(rb"(?:^|\n)@@S%d@@\r?\n([\s\S]*?)@@%d@@(\d+)" % (n, n), timeout)
        out = m.group(1).decode(errors="replace").replace("\r", "")
        rc = int(m.group(2))
        if check and rc:
            raise RuntimeError(f"`{cmd}` failed (rc={rc}): {out.strip()[-300:]}")
        return rc, out


def _get_file(con, path, size_hint=0):
    """Reads a remote file through `base64`, decoding as it streams."""
    con.n += 1
    n = con.n
    tag = f"@@{n}@@".encode()
    con.buf = b""
    con.send(f'echo "@@S{n}@@"; base64 {path}; echo "@@{n}@@$?"')
    con.expect(rb"(?:^|\n)@@S%d@@\r?\n" % n, 20)  # skip the (possibly wrapped) echo
    out = bytearray()
    pending = con.buf
    con.buf = b""
    t0 = last = time.time()
    while True:
        data = con.s.read(65536)
        if data:
            con.log.write(data)
            pending += data
        *lines, pending = pending.split(b"\n")
        for ln in lines:
            ln = ln.strip()
            if ln.startswith(tag):
                rc = int(ln[len(tag):] or b"1")
                if rc:
                    raise RuntimeError(f"base64 {path} failed (rc={rc})")
                return bytes(out)
            if ln:
                out += base64.b64decode(ln)
        if size_hint and time.time() - last > 30:
            last = time.time()
            rate = len(out) / (last - t0)
            say(f"  {len(out) * 100 // size_hint} % ({len(out) // 1024} KiB, "
                f"~{(size_hint - len(out)) / max(rate, 1) / 60:.0f} min restantes)")


def _send_b64(con, data, path):
    """Writes bytes to a remote file through `base64 -d`, using the tty echo
    as flow control (a batch of lines is sent once the previous one is back)."""
    text = base64.encodebytes(data).decode().splitlines()
    con.buf = b""
    con.send(f"base64 -d > {path}")
    con.expect(rb"\n", 10)
    sent = echoed = 0
    batch = 24
    last = time.time()
    while sent < len(text):
        chunk = text[sent:sent + batch]
        con.s.write(("\n".join(chunk) + "\n").encode())
        sent += len(chunk)
        while echoed < sent:
            data_in = con.s.read(4096)
            if data_in:
                con.log.write(data_in)
                echoed += data_in.count(b"\n")
                last = time.time()
            elif time.time() - last > 20:
                raise TimeoutError("no echo from the camera while sending data")
    con.s.write(b"\x04")  # end of input for base64 -d
    time.sleep(0.3)
    con.buf = b""


def _put_file(con, data, path, block=32768):
    """Sends a file in md5-checked blocks: the serial line has no flow control
    and an occasional character can be altered on the way to the camera."""
    con.run(f": > {path}")
    t0 = last = time.time()
    for off in range(0, len(data), block):
        part = data[off:off + block]
        for attempt in range(1, 6):
            _send_b64(con, part, "/tmp/.blk")
            _, out = con.run("md5sum /tmp/.blk", timeout=60, check=False)
            if out.split() and out.split()[0] == md5(part):
                break
            say(f"  bloc {off // block} altéré en transmission, renvoi ({attempt})")
        else:
            raise RuntimeError("liaison série trop bruitée (5 échecs sur un même bloc) : vérifier les fils")
        con.run(f"cat /tmp/.blk >> {path} && rm /tmp/.blk")
        if len(data) > 1 << 20 and time.time() - last > 30:
            last = time.time()
            done = off + len(part)
            say(f"  {done * 100 // len(data)} % envoyés, "
                f"~{(len(data) - done) * (last - t0) / done / 60:.0f} min restantes")
    rc, out = con.run(f"md5sum {path}", timeout=120)
    got = out.split()[0] if out.split() else ""
    if got != md5(data):
        raise RuntimeError(f"{path}: md5 {got} != {md5(data)}")


def catch_uboot(con, timeout):
    """Floods Ctrl-B while the camera powers up: the vendor u-boot has
    bootdelay=0 and checks the console only once. Returns 'vendor' (stopped
    at PRJ007#), 'thingino' or 'missed' (the kernel started)."""
    con.s.timeout = 0
    con.buf = b""
    end = time.time() + timeout
    try:
        while time.time() < end:
            con.s.write(b"\x02" * 8)
            time.sleep(0.002)
            con._read()
            if b"PRJ007#" in con.buf:
                return "vendor"
            if re.search(rb"U-Boot 20[2-9]\d", con.buf):
                return "thingino"
            if b"Starting kernel" in con.buf:
                return "missed"
    finally:
        con.s.timeout = 0.05
    raise TimeoutError("aucune sortie u-boot : vérifier le câble série (TX/RX croisés ?) et l'alimentation")


def step_uboot(con, args):
    for attempt in range(1, 4):
        if args.power_cmd:
            if args.power_off_cmd:
                subprocess.run(args.power_off_cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                time.sleep(5)
            say(f"Mise sous tension de la caméra (essai {attempt})…")
            subprocess.Popen(args.power_cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            say("Mettez la caméra sous tension maintenant (ou débranchez/rebranchez-la).")
        state = catch_uboot(con, 180)
        if state == "vendor":
            break
        if state == "thingino":
            sys.exit("Cette caméra démarre déjà sur le u-boot thingino : utilisez la page web "
                     "Config > Mise à jour, ou --post-only.")
        say("u-boot pas interrompu à temps (le noyau a démarré).")
        if not (args.power_cmd and args.power_off_cmd):
            sys.exit("Débranchez la caméra, relancez le script, puis rebranchez-la quand il le demande.")
    else:
        sys.exit("Impossible d'interrompre le u-boot après 3 essais.")
    time.sleep(0.5)
    con.s.reset_input_buffer()
    con.buf = b""
    con.send("")
    con.expect(rb"PRJ007# ", 5)
    con.buf = b""
    con.send("printenv")
    m = con.expect(rb"([\s\S]*?)PRJ007# ", 10)
    env = m.group(1).decode(errors="replace").replace("\r", "")
    (args.backup / "uboot-env.txt").write_text(env)
    kv = dict(re.findall(r"^(\w+)=(.*)$", env, re.M))
    mac, sensor, soc = kv.get("ethaddr") or kv.get("mac", ""), kv.get("SENSOR", ""), kv.get("TYPE", "")
    say(f"u-boot vendor : MAC {mac}, capteur {sensor}, SoC {soc}, carte {kv.get('BOARD_TYPE', '?')}")
    if not sensor.startswith("gc8602") or soc != "T32NQ":
        sys.exit("Matériel différent de la WinEye Pro de référence (GC8602 / T32NQ) : arrêt, rien n'a été modifié.")
    (args.backup / "mac.txt").write_text(mac + "\n")
    for c in UBOOT_SHELL:
        con.send(c)
        if not c.startswith("bootm"):
            con.expect(rb"PRJ007# ", 30)
    return mac


def step_vendor_shell(con):
    say("Démarrage du noyau vendor avec un shell nu…")
    con.expect(rb"[#$] $|can't access tty", 120)
    time.sleep(2)
    con.send("")
    con.expect(rb"[#$] $", 10)
    con.run("mount -t proc proc /proc 2>/dev/null; mount -t sysfs sysfs /sys 2>/dev/null; "
            "mount -t tmpfs tmpfs /tmp; echo 1 > /proc/sys/kernel/printk")
    con.run("[ -e /dev/mtd6 ] || mount -t devtmpfs devtmpfs /dev")
    check_mtd(con)


def check_mtd(con):
    _, mtd = con.run("cat /proc/mtd")
    sizes = re.findall(r"^mtd(\d): ([0-9a-f]+)", mtd, re.M)
    want = {str(n): f"{s:08x}" for n, (_, s) in VENDOR_MTD.items()}
    want["6"] = f"{FLASH_SIZE:08x}"
    if dict(sizes) != want:
        sys.exit(f"Partitions inattendues :\n{mtd}")


def step_vendor_wifi(con, args):
    """Reads the Wi-Fi network of the vendor firmware (config partition), so
    thingino can join the same network without typing its password."""
    con.run("mkdir -p /tmp/cfg && mount -t jffs2 -o ro /dev/mtdblock4 /tmp/cfg", check=False)
    _, conf = con.run("cat /tmp/cfg/wpa_supplicant.conf 2>/dev/null", check=False)
    con.run("umount /tmp/cfg", check=False)
    ssid = re.search(r'^\s*ssid="(.*)"\s*$', conf, re.M)
    psk = re.search(r'^\s*psk=("(.*)"|([0-9a-fA-F]{64}))\s*$', conf, re.M)
    if ssid and psk:
        (args.backup / "wifi-vendor.txt").write_text(f"{ssid.group(1)}\n{psk.group(1)}\n")
        say(f"WiFi du firmware d'origine : « {ssid.group(1)} » (repris par thingino)")
    else:
        say("WiFi du firmware d'origine introuvable : utilisez --wifi-ssid")


def wifi_psk(args):
    """(ssid, 64-hex PSK) from --wifi-ssid/--wifi-pass or the vendor config."""
    if args.wifi_ssid:
        return args.wifi_ssid, hashlib.pbkdf2_hmac(
            "sha1", args.wifi_pass.encode(), args.wifi_ssid.encode(), 4096, 32).hex()
    f = args.backup / "wifi-vendor.txt"
    if not f.exists():
        return None, None
    ssid, psk = f.read_text().split("\n")[:2]
    if psk.startswith('"'):
        psk = hashlib.pbkdf2_hmac("sha1", psk.strip('"').encode(), ssid.encode(), 4096, 32).hex()
    return ssid, psk


def step_backup(con, args):
    path = args.backup / "vendor-flash-16M.bin"
    say("Sauvegarde de la flash vendor vers le PC (~35 min)…")
    _, out = con.run("md5sum /dev/mtd6", timeout=120)
    want = out.split()[0]
    for attempt in (1, 2):
        data = _get_file(con, "/dev/mtd6", FLASH_SIZE)
        if len(data) == FLASH_SIZE and md5(data) == want:
            path.write_bytes(data)
            (args.backup / "vendor-flash-16M.md5").write_text(f"{want}  {path.name}\n")
            say(f"Sauvegarde OK : {path} (md5 {want})")
            return
        say(f"Sauvegarde incomplète ou corrompue (essai {attempt}), on recommence…")
    sys.exit("Sauvegarde impossible : rien n'a été écrit. Vérifiez le câble série et relancez.")


def step_iq(con, args):
    d = args.backup / "iq"
    d.mkdir(exist_ok=True)
    for name in IQ_FILES:
        _, out = con.run(f"md5sum /lib/modules/{name}")
        data = _get_file(con, f"/lib/modules/{name}")
        if md5(data) != out.split()[0]:
            sys.exit(f"Copie de {name} corrompue : relancez.")
        (d / name).write_bytes(data)
    say(f"Fichiers de calibration (IQ) copiés dans {d}")


def step_flash(con, image):
    _, out = con.run("md5sum /tmp/image.bin 2>/dev/null", timeout=120, check=False)
    if out.split() and out.split()[0] == md5(image):
        say("Image déjà présente en RAM (md5 OK) : envoi sauté")
    else:
        say("Envoi de l'image thingino à la caméra (~17 min)…")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
            z.writestr("image.bin", image)
        _put_file(con, buf.getvalue(), "/tmp/image.zip")
        con.run("cd /tmp && unzip -o -q image.zip && rm image.zip && cd /", timeout=120)
        _, out = con.run("md5sum /tmp/image.bin", timeout=120)
        if out.split()[0] != md5(image):
            sys.exit("Image corrompue après décompression : rien n'a été écrit, relancez.")
    # Writing mtd2 replaces the rootfs we are running from: do everything with
    # a busybox and its libraries copied to RAM.
    con.run("mkdir -p /tmp/rt/bin /tmp/rt/lib /tmp/rt/dev /tmp/rt/tmp && cp /bin/busybox /tmp/rt/bin/ && "
            "cp -L " + " ".join(f"/lib/{lib}" for lib in VENDOR_LIBS) + " /tmp/rt/lib/ && "
            "cd /tmp/rt/lib && ln -sf ld-uClibc-0.9.33.2.so ld-uClibc.so.0 && "
            "ln -sf libuClibc-0.9.33.2.so libc.so.0 && ln -sf libm-0.9.33.2.so libm.so.0 && cd / && "
            "mount --bind /dev /tmp/rt/dev && mount --bind /tmp /tmp/rt/tmp")
    con.send("chroot /tmp/rt /bin/busybox sh")
    time.sleep(1)
    con.run("export PATH=/bin; busybox --install -s /bin 2>/dev/null; :")
    say("Écriture de la flash (u-boot d'abord, ~2 min) — NE PAS COUPER L'ALIMENTATION")
    for n in WRITE_ORDER:
        off, size = VENDOR_MTD[n]
        part = image[off:off + size]
        con.run(f"busybox dd if=/tmp/image.bin of=/tmp/p{n}.bin bs=32768 skip={off // 32768} "
                f"count={size // 32768} 2>/dev/null", timeout=60)
        for attempt in (1, 2):
            con.run(f"busybox flashcp /tmp/p{n}.bin /dev/mtd{n}", timeout=600, check=False)
            _, out = con.run(f"busybox dd if=/dev/mtd{n} bs=32768 2>/dev/null | busybox md5sum", timeout=120)
            if out.split()[0] == md5(part):
                say(f"  mtd{n} écrit et vérifié")
                break
            say(f"  mtd{n} : vérification échouée (essai {attempt})")
        else:
            sys.exit(f"ÉCHEC d'écriture de mtd{n}. NE PAS ÉTEINDRE : relancez le script ou suivez "
                     "« Récupération » dans le README (u-boot thingino + loady).")
        con.run(f"busybox rm -f /tmp/p{n}.bin")
    say("Flash écrite et vérifiée. Redémarrage sur thingino…")
    con.send("busybox reboot -f")


def login(con, password, first_password):
    con.expect(rb"login: ", 240, poke=None)
    con.send("root")
    con.expect(rb"Password: ", 20)
    con.send(first_password)
    m = con.expect(rb"Password: |[#$] $|Login incorrect", 30)
    if m.group(0).startswith(b"Login incorrect"):
        raise RuntimeError("mot de passe root refusé")
    if m.group(0) == b"Password: ":  # thingino first login: choose a password
        con.send(password)
        con.expect(rb"Confirm: ", 10)
        con.send(password)
        con.expect(rb"[#$] $", 30)
    con.run("dmesg -n 1")
    pw = password.replace("'", "'\\''")
    con.run(f"echo 'root:{pw}' | chpasswd -c sha512 >/dev/null")


def step_post(con, args, mac, iq_dir):
    say("Premier démarrage de thingino (~1 min)…")
    login(con, args.root_password, "password")
    for name in IQ_FILES:
        f = iq_dir / name
        if f.exists():
            _put_file(con, f.read_bytes(), f"/usr/share/sensor/{name}")
    say("Calibration du capteur installée")
    if mac:
        con.run(f"jct /etc/thingino.json set wlan.mac {mac}")
        say(f"MAC d'origine restaurée : {mac}")
    ssid, psk = wifi_psk(args)
    if ssid:
        q = ssid.replace("'", "'\\''")
        con.run(f"wlan configure '{q}' {psk}")
        say(f"WiFi configuré : {ssid}")
    if args.hostname:
        con.run(f"echo {args.hostname} > /etc/hostname && sed -i '/^127.0.1.1/c127.0.1.1\\t{args.hostname}' /etc/hosts")
    if args.format_emmc:
        say("Formatage de l'eMMC en exFAT (efface les enregistrements vendor)…")
        con.run("umount /mnt/mmcblk0p1 2>/dev/null; printf 'o\\nu\\nn\\np\\n1\\n2048\\n\\nt\\n7\\nw\\n' | "
                "fdisk /dev/mmcblk0 >/dev/null 2>&1; mdev -s; sleep 2; mkfs.exfat -n CAMERA /dev/mmcblk0p1",
                timeout=300)
    con.run("sync")
    con.send("reboot")
    say("Redémarrage final…")
    login(con, args.root_password, args.root_password)
    ip = ""
    for _ in range(30):
        _, out = con.run("ip -4 -o addr show wlan0 2>/dev/null", check=False)
        m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", out)
        if m:
            ip = m.group(1)
            break
        time.sleep(3)
    _, iq = con.run("ls /usr/share/sensor/", check=False)
    _, video = con.run("pidof rvd >/dev/null && echo running || echo stopped", check=False)
    print("\n================ INSTALLATION TERMINÉE ================")
    print(f"  Vidéo (raptor)  : {video.strip()}   IQ : {'OK' if 'day' in iq else 'ABSENTS'}")
    if ip:
        print(f"  Interface web   : https://{ip}/   (root / {'<votre mot de passe>' if args.root_password != 'password' else 'password'})")
        print(f"  Flux RTSP       : rtsp://<utilisateur>:<mot de passe>@{ip}:554/ch0   (image publique : admin / password)")
    else:
        print("  Pas d'adresse IP : configurez le WiFi (relancez avec --post-only --wifi-ssid …)")
    print(f"  Sauvegarde vendor : {args.backup}")
    print("=======================================================")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", default="/dev/ttyUSB0", help="adaptateur USB-série 3,3 V (défaut /dev/ttyUSB0)")
    p.add_argument("--image", default="showmo-wineye-thingino.bin", help="image thingino 16 Mo")
    p.add_argument("--backup", default=None, help="dossier de sauvegarde (défaut ./backup-<date>)")
    p.add_argument("--wifi-ssid", help="réseau WiFi à configurer")
    p.add_argument("--wifi-pass", help="mot de passe WiFi (demandé si absent)")
    p.add_argument("--hostname", help="nom réseau de la caméra")
    p.add_argument("--root-password", default="password", help="mot de passe root (web, SSH) ; défaut password")
    p.add_argument("--format-emmc", action="store_true", help="formater l'eMMC en exFAT (efface le contenu vendor)")
    p.add_argument("--post-only", action="store_true",
                   help="caméra déjà flashée : seulement la configuration (IQ, MAC, WiFi…)")
    p.add_argument("--backup-only", action="store_true",
                   help="s'arrêter après la sauvegarde et la copie des IQ (rien n'est écrit) ; "
                        "la caméra reste dans le shell vendor pour --resume-flash")
    p.add_argument("--resume-flash", action="store_true",
                   help="reprendre après --backup-only : écriture puis configuration (même --backup)")
    p.add_argument("--power-cmd", help="commande qui met la caméra sous tension (relais…)")
    p.add_argument("--power-off-cmd", help="commande qui coupe la caméra (pour réessayer seul)")
    args = p.parse_args()

    from pathlib import Path
    args.backup = Path(args.backup or f"backup-{time.strftime('%Y%m%d-%H%M%S')}")
    args.backup.mkdir(parents=True, exist_ok=True)
    if args.wifi_ssid and not args.wifi_pass:
        args.wifi_pass = getpass.getpass(f"Mot de passe du WiFi « {args.wifi_ssid} » : ")
    if args.hostname and not re.fullmatch(r"[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?", args.hostname):
        sys.exit("--hostname : lettres, chiffres et tirets seulement")

    log = open(args.backup / "serial.log", "ab")
    con = Console(args.port, log)
    if args.post_only:
        iq_dir = args.backup / "iq"
        if not all((iq_dir / n).exists() for n in IQ_FILES):
            sys.exit(f"--post-only : il faut les fichiers IQ dans {iq_dir} (--backup <dossier de la 1re passe>)")
        mac_file = args.backup / "mac.txt"
        mac = mac_file.read_text().strip() if mac_file.exists() else ""
        say("Redémarrez la caméra (débrancher/rebrancher)…")
        step_post(con, args, mac, iq_dir)
        return

    image = Path(args.image).read_bytes()
    if len(image) != FLASH_SIZE or image[0x70000:0x70004] != b"\x27\x05\x19\x56" or image[0x200000:0x200004] != b"hsqs":
        sys.exit(f"{args.image} n'est pas une image thingino complète de 16 Mo")
    say(f"Image {args.image} : md5 {md5(image)}")
    if args.resume_flash:
        iq_dir = args.backup / "iq"
        if not (args.backup / "vendor-flash-16M.bin").exists() or not all((iq_dir / n).exists() for n in IQ_FILES):
            sys.exit(f"--resume-flash : sauvegarde ou IQ absents de {args.backup}")
        mac = (args.backup / "mac.txt").read_text().strip()
        con.send("")
        con.expect(rb"[#$] $", 10)
        # A previous run may have stopped inside the RAM chroot (no /proc there):
        # leave it. Never send "exit" to the bare init shell (kernel panic).
        _, where = con.run("[ -d /proc/1 ] && echo HOST || echo CHROOT", check=False)
        if "CHROOT" in where:
            con.send("exit")
            time.sleep(1)
            con.send("")
            con.expect(rb"[#$] $", 10)
        check_mtd(con)
        step_flash(con, image)
        step_post(con, args, mac, iq_dir)
        return
    mac = step_uboot(con, args)
    step_vendor_shell(con)
    step_backup(con, args)
    step_iq(con, args)
    step_vendor_wifi(con, args)
    if args.backup_only:
        say(f"Sauvegarde terminée dans {args.backup} ; rien n'a été écrit. La caméra reste dans le shell "
            f"vendor : relancer avec --resume-flash --backup {args.backup} pour écrire.")
        return
    step_flash(con, image)
    step_post(con, args, mac, args.backup / "iq")


if __name__ == "__main__":
    main()
