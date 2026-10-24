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
  - *Config* : débit et i/s (bouton **Défaut**), horodatage (taille, fuseau), **fortes lumières** (atténuation du halo 0–128 et récupération des ombres / DRC 64–255), identifiants vidéo, nom réseau, WiFi, mot de passe root et clés SSH, redémarrage, mise à jour du firmware.
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
python3 install.py --port /dev/ttyUSB0 --image showmo-wineye-thingino-v15.bin --format-emmc
```
Quand le script le demande, **mettre la caméra sous tension**. Il fait tout seul :
1. arrêt du u-boot d'origine, contrôle du matériel, lecture de l'adresse MAC ;
2. **sauvegarde complète de la flash d'origine** vers le PC (~35 min, vérifiée par md5) ;
3. copie des fichiers de calibration du capteur et du réseau WiFi depuis le firmware d'origine (ils ne sont pas dans l'image) ;
4. envoi de l'image (~17 min, par blocs vérifiés) et écriture, u-boot d'abord, chaque partition relue et vérifiée ;
5. premier démarrage de thingino : mot de passe root, MAC d'origine, calibration, WiFi, eMMC ;
6. redémarrage et affichage de l'adresse IP.

Options utiles :
- `--wifi-ssid "<réseau>"` (mot de passe demandé) : un autre réseau que celui du firmware d'origine, repris par défaut ;
- `--root-password <mdp>` (sinon `password`), `--hostname <nom>` ;
- `--format-emmc` : sans lui, l'eMMC garde son format d'origine (ext4), illisible par thingino ;
- `--backup-only` puis `--resume-flash --backup <dossier>` : s'arrêter après la sauvegarde (rien n'est écrit, la caméra attend dans
  le shell d'origine), vérifier, puis lancer l'écriture.

**Ne pas couper l'alimentation pendant l'écriture** (étape 4, ~2 min, annoncée par le script).

### 3. Après l'installation
1. Ouvrir `https://<ip>/` (certificat auto-signé : accepter l'avertissement), se connecter en `root` / `password`.
2. **Config › Compte root** : nouveau mot de passe. **Config › Identifiants vidéo** : nouveaux identifiants RTSP.
3. Si un projecteur ou des phares éblouissent l'image (halo), **Config › Fortes lumières** : l'atténuation vaut 64 par défaut ;
   la monter (jusqu'à 128) réduit le halo mais assombrit le reste, ce que la **récupération des ombres (DRC)** compense
   (exemple testé de nuit : atténuation 128 + DRC 160). DRC 128 = réglage d'usine ; trop fort, il éclaircit aussi le halo.
4. Blue Iris : caméra « Generic/ONVIF », RTSP port 554, flux principal `/ch0`, sous-flux vide, transport TCP :
   `rtsp://<utilisateur>:<mot de passe>@<ip>:554/ch0`.
5. Réserver l'adresse IP dans le routeur (DHCP) et, si plusieurs points d'accès diffusent le même réseau, préférer le plus proche.

Si une étape après l'écriture échoue (WiFi, coupure du câble…), relancer seulement la fin :
`python3 install.py --post-only --backup backup-AAAAMMJJ-HHMMSS`.

### Dépannage
- **« u-boot pas interrompu à temps »** à chaque essai alors que le démarrage s'affiche : la caméra ne reçoit rien du PC.
  Vérifier le fil `TX` de l'adaptateur → `RX` de la caméra (soudure, croisement).
- **« bloc … altéré en transmission »** de temps en temps : normal, le bloc est renvoyé. Si un bloc échoue 5 fois, la liaison est
  trop bruitée : refaire les soudures, raccourcir les fils.
- Une erreur **avant** « Écriture de la flash » ne modifie rien : relancer (`--resume-flash --backup <dossier>` si la sauvegarde est
  faite, la caméra étant restée dans le shell d'origine). Une erreur **pendant** l'écriture : ne pas couper l'alimentation, relancer
  `--resume-flash` (l'image déjà envoyée n'est pas renvoyée).

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
- `install.py` a été validé de bout en bout sur une 2e caméra neuve (firmware d'origine → thingino v13, 2026-10-23).

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
