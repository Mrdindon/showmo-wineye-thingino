# Showmo WinEye Pro → thingino

Firmware libre [thingino](https://github.com/themactep/thingino-firmware) pour la caméra de fenêtre
**Showmo WinEye Pro** (4K, eMMC), sans cloud, prévu pour un NVR comme **Blue Iris**.

| Matériel reconnu | |
|---|---|
| Carte | Puwell **PRJ007** (`BOARD_TYPE=PW_BOARD_WINEYE`) |
| SoC | Ingenic **T32NQ** |
| Capteur | GalaxyCore **GC8602** (8 MP) |
| WiFi | AICSemi **AIC8800D81** (USB) |
| Flash / stockage | SPI NOR 16 Mo (XM25QH128C) / eMMC 64 Go |

Le script d'installation vérifie le capteur et le SoC et s'arrête sans rien écrire si la caméra est différente.

## Ce que fait le firmware
- **Un flux RTSP H.264** 2560×1440, 25 i/s, 15 Mb/s CBR, audio AAC, avec la date/heure incrustée.
- **Interface web à 2 pages** (`https://<ip>/`) :
  - *Direct* : aperçu à 1 image/s en pleine résolution, photo, URL RTSP ;
  - *Config* : débit et i/s (bouton **Défaut**), horodatage (taille, fuseau), identifiants vidéo, nom réseau, WiFi, mot de passe root et clés SSH, redémarrage, mise à jour du firmware.
- **Bouton reset** : appui court = la caméra annonce son IP sur le haut-parleur ; 5 s = efface le WiFi ; 20 s = retour aux réglages d'usine.
- Image couleur jour et nuit (la WinEye n'a pas d'infrarouge : objectif F1.0), ONVIF, NTP, SSH, eMMC en exFAT.
- Aucun accès Internet nécessaire, aucun cloud.

## Identifiants par défaut — à changer
| Usage | Utilisateur | Mot de passe | Où le changer |
|---|---|---|---|
| Interface web, SSH, console série | `root` | `password` | Config › Compte root |
| Flux RTSP (et aperçu) | `admin` | `password` | Config › Identifiants vidéo |

## Installation depuis le firmware d'origine
### Ce qu'il faut
- La caméra **sous son firmware Showmo d'origine**.
- Un **adaptateur USB-série 3,3 V** (FTDI, CP2102, CH340… **pas de 5 V**) et de quoi souder 3 fils.
- Un PC Linux ou macOS avec `python3` et `pyserial` (`pip install pyserial`).
- L'image : `showmo-wineye-thingino-vNN.bin` dans les *Releases* de ce dépôt (vérifier son sha256 avec `SHA256SUMS`).

### 1. Brancher la console série
Caméra **débranchée**, ouvrir le boîtier et souder 3 fils sur les pastilles de l'**UART1** de la carte PRJ007 :
`TX` caméra → `RX` adaptateur, `RX` caméra → `TX` adaptateur, `GND` → `GND`. **Ne pas** brancher le 3,3 V de l'adaptateur.
<!-- TODO photo des pastilles UART -->
Vérification (facultative) : `picocom -b 115200 /dev/ttyUSB0` puis brancher la caméra : le démarrage s'affiche.

### 2. Lancer l'installation (~1 h)
```sh
python3 install.py --port /dev/ttyUSB0 --image showmo-wineye-thingino-v13.bin \
        --wifi-ssid "MonWiFi" --format-emmc
```
Quand le script le demande, **mettre la caméra sous tension**. Il fait tout seul :
1. arrêt du u-boot d'origine, contrôle du matériel, lecture de l'adresse MAC ;
2. **sauvegarde complète de la flash d'origine** vers le PC (~35 min, vérifiée par md5) ;
3. copie des fichiers de calibration du capteur depuis le firmware d'origine (ils ne sont pas dans l'image) ;
4. envoi de l'image (~15 min) et écriture, u-boot d'abord, chaque partition relue et vérifiée ;
5. premier démarrage de thingino : mot de passe root, MAC d'origine, calibration, WiFi, eMMC ;
6. redémarrage et affichage de l'adresse IP.

Options utiles : `--root-password <mdp>` (sinon `password`), `--hostname <nom>`, sans `--wifi-ssid` le WiFi est à régler plus tard
(`--post-only`). Sans `--format-emmc`, l'eMMC garde son format d'origine (ext4), illisible par thingino.

**Ne pas couper l'alimentation pendant l'écriture** (étape 4, ~2 min, annoncée par le script).

### 3. Après l'installation
1. Ouvrir `https://<ip>/` (certificat auto-signé : accepter l'avertissement), se connecter en `root` / `password`.
2. **Config › Compte root** : nouveau mot de passe. **Config › Identifiants vidéo** : nouveaux identifiants RTSP.
3. Blue Iris : caméra « Generic/ONVIF », RTSP port 554, flux principal `/ch0`, sous-flux vide, transport TCP :
   `rtsp://<utilisateur>:<mot de passe>@<ip>:554/ch0`.
4. Réserver l'adresse IP dans le routeur (DHCP) et, si plusieurs points d'accès diffusent le même réseau, préférer le plus proche.

Si une étape après l'écriture échoue (WiFi, coupure du câble…), relancer seulement la fin :
`python3 install.py --post-only --backup backup-AAAAMMJJ-HHMMSS --wifi-ssid "MonWiFi"`.

## Mises à jour
Interface web › Config › **Mise à jour du firmware** : envoyer l'image complète d'une nouvelle *Release*. Seuls le noyau et le système sont
réécrits (u-boot, réglages et calibration conservés), chacun vérifié par md5, puis la caméra redémarre (~2 min).

## Sauvegarde et retour au firmware d'origine
Le dossier `backup-…/` créé par `install.py` contient `vendor-flash-16M.bin` (+ md5), la calibration (`iq/`), la MAC et l'environnement u-boot.
**Le garder en lieu sûr et ne pas le publier** : la flash d'origine contient les clés cloud propres à la caméra.

Retour au firmware d'origine (console série) : interrompre le u-boot thingino au démarrage, puis
```
loady 0x80800000            # envoyer vendor-flash-16M.bin en YMODEM (~25 min)
sf probe; sf erase 0 0x1000000; sf write 0x80800000 0 0x1000000; reset
```

## Reconstruire l'image
```sh
./build.sh          # clone thingino (commit 9925f46), applique patches/*.patch, construit
```
Hôte Linux, ~40 Go libres, outils de build thingino + `lzop`. L'image produite ne contient aucun fichier du constructeur.

## Limites connues
- **4K abandonnée** : en 3840×2160 à 25 i/s, l'encodeur perd 1–2 images après chaque image clé (saccade chaque seconde) ; 2560×1440 est fluide.
- Pas d'aperçu vidéo fluide dans le navigateur (WebRTC impossible faute de mémoire) : l'aperçu est à 1 image/s, la vidéo complète passe par RTSP.
- Horodatage : uniquement via l'OSD du processeur d'image (celui de l'encodeur bloque la caméra en mode IVDC) ; régler sa taille dans la page Config, pas ailleurs.
- eMMC en exFAT sans vérification au démarrage (`fsck` absent).
- `install.py` reproduit la procédure validée manuellement sur la caméra de référence, mais n'a pas encore été exécuté d'un bout à l'autre
  sur une caméra neuve : garder la console série ouverte la première fois et signaler tout écart.

## Contenu du dépôt
| Fichier | Rôle |
|---|---|
| `install.py` | installation tout-série depuis le firmware d'origine |
| `build.sh` | reconstruction de l'image depuis les sources thingino |
| `patches/` | modifications de thingino (profil caméra, interface web, correctifs) |
| `SHA256SUMS` | empreintes des images publiées |

## Crédits
[thingino](https://github.com/themactep/thingino-firmware), [raptor](https://github.com/gtxaspec/raptor),
[thingino/ingenic-sdk](https://github.com/thingino/ingenic-sdk), [aic8800-wifi](https://github.com/gtxaspec/aic8800-wifi),
discussion thingino [#1157](https://github.com/themactep/thingino-firmware/discussions/1157) (portage T32/PRJ007).
Les patches suivent les licences des projets qu'ils modifient. Showmo et Puwell sont des marques de leurs propriétaires ; ce projet n'est pas affilié.
