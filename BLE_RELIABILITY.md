# Incident BLE du 5 octobre 2026 — diagnostic et fiabilisation

Audit du 19 octobre 2026. Bases examinées : `ch.bus.bluetooth-mqtt` à
`93194dd` et `ch.bus.burry-gps` à `d768e02`, puis corrections locales sur
`codex/ble-reliability` dans chaque dépôt. La pièce jointe est la seule source
des constats de production : aucun accès SSH, redémarrage, déploiement ni
modification du système de `hiva-oa` n'a été effectué. Les sources actuelles
ne prouvent pas les versions exactes exécutées lors de l'incident.

## 1. Conclusion et niveau de certitude

L'incident est compatible avec une perte durable des observations de la chaîne
BLE. Les répétitions de `BLE scanner started` prouvent le retour des appels
de démarrage, **pas le retour des annonces radio**. Les mesures GPIO et les
requêtes GPS encore traitées rendent un blocage global de Linux moins probable.
La cause racine et l'implication du GPS ne sont pas démontrées.

Classement qualitatif des pistes à vérifier, sans probabilité numérique :

| Rang | Piste | Éléments favorables | Limites |
| --- | --- | --- | --- |
| 1 | Chaîne de réception BLE dégradée : état découverte/BlueZ, transport D-Bus ou contrôleur | Plusieurs capteurs cessent d'actualiser ensemble ; watchdog sans reprise des annonces pendant environ une heure | Aucun `btmon`, état D-Bus ou état HCI pendant la panne ; impossible de départager ces couches |
| 2 | Interactions scan/connexion et reconnexions GPS comme facteur aggravant | Deux clients sur `hci0` ; GPS avec adresse brute entraînant une recherche implicite supplémentaire ; reconnexions périodiques | BlueZ autorise plusieurs clients ; aucune corrélation des logs GPS et aucune reproduction comparative ; cause possible seulement |
| 3 | Conditions radio, interférences 2,4 GHz, alimentation ou état firmware | Plausible sur un équipement embarqué ; power-cycle efficace pour le problème de reboot | Aucune mesure radio, tension, sous-voltage ou trace firmware fournie ; reboot possiblement indépendant |
| 4 | Panne globale MQTT/Docker/Linux | Peut expliquer des mesures figées en général | Moins cohérent avec les services encore actifs et surtout avec le silence des callbacks détecté par le watchdog |

La faiblesse du watchdog est avérée : il ne sait pas établir la réussite de sa
récupération. Cela explique la persistance du cycle, pas son déclenchement.
Une erreur GATT isolée n'explique pas à elle seule la disparition des annonces
Victron indépendantes des lectures GATT. L'absence d'erreur dans le journal ne
prouve pas le bon fonctionnement du chemin de données.

Les heures `12:09`/`12:11` UTC correspondent à `14:09`/`14:11` CEST le 5 octobre.
Ne pas mélanger ce fuseau avec les extraits de l'arrêt système à 15:12.

## 2. Audit du code avant correction

### Passerelle Bluetooth → MQTT

- `bluetooth_gateway/service.py` possède une seule boucle de cycle de vie.
  Les callbacks décodent et mémorisent les dernières annonces ; ils ne lancent
  pas de tâches GATT. Victron est déchiffré localement, sans scanner tiers.
- Le scanner est arrêté avant les lectures Inkbird. Cette pause concerne la
  session de la passerelle ; si le GPS découvre aussi, elle ne garantit pas
  l'arrêt physique de toute découverte BlueZ.
- Le `BLEDevice` est déjà transmis au client Inkbird. Connexion, lecture et
  déconnexion sont bornées ; `finally` déconnecte aussi une connexion partielle.
  Les échecs GATT ordinaires sont isolés par sonde et nombre de tentatives.
- Un démarrage expiré ou un nettoyage non confirmé provoque déjà une sortie,
  pour éviter une acquisition avec propriété D-Bus incertaine. Ce choix est
  conservé. Bleak 3.0.2 n'installe son handle d'arrêt qu'après `active_scan` et
  le retire avant d'attendre son arrêt : répéter `stop()` ne prouve rien.
- Le watchdog était basé sur l'absence de **toute** annonce, avec reprise toutes
  les 120 secondes. Le démarrage réinitialisait la temporisation et le backoff,
  même sans observation. Les échecs de démarrage pouvaient durer indéfiniment.
- Les absences de sondes augmentent les cycles manqués ; les topics existants
  `availability` et le bilan `scan` signalent les appareils manquants.
- `publishing.py` utilise un thread réseau Paho indépendant, QoS 1 et retain,
  un tampon de dernières valeurs par topic et une file réseau bornée.
  Une panne MQTT ne doit donc pas provoquer de récupération BLE.
- Les exceptions de décodage/GATT sont journalisées par type et appareil avec
  limitation de fréquence ; leur contenu n'est pas interpolé, pour protéger
  les secrets. SIGTERM annule la boucle et attend son nettoyage.
- Le verrou `owner.lock` ne concerne que les instances de cette passerelle.
  Il n'est pas partagé avec le GPS et n'est pas un verrou de tout `hci0`.

### GPS ESP32 → RabbitMQ

- `BleakScanner.discover()` crée un scanner temporaire dans un contexte async,
  attend la fenêtre de découverte puis l'arrête. Son utilisation seule ne
  prouve donc pas une fuite. L'appel n'avait toutefois pas de garde externe
  sur le démarrage et le nettoyage D-Bus.
- `find_device()` renvoyait une adresse ; `BleakClient(address)` pouvait lancer
  sa propre découverte implicite. L'adaptateur GPS n'était pas explicite.
- Une seule boucle GPS effectuait les opérations successivement, mais la
  connexion en contexte `async with` ne protégeait pas explicitement le cas
  d'un `__aenter__` partiellement réussi. L'abonnement et le nettoyage n'avaient
  pas de délai applicatif distinct.
- L'absence de GPS pendant 900 secondes provoquait un `TimeoutError` fatal ;
  avec une politique Docker de redémarrage, cela relançait inutilement le
  processus et RabbitMQ lorsque le contact était coupé.
- Après connexion, `disconnected_since=None`. Un timeout de `start_notify`
  passait par le bloc `except TimeoutError` sans restaurer cette valeur : le
  calcul suivant pouvait échouer par soustraction de `None`.
- Le callback de notification appelait directement `channel.basic_publish`
  de Pika synchrone : le réseau RabbitMQ pouvait immobiliser la boucle BLE.
  Toute erreur JSON ou RabbitMQ provoquait aussi `sys.exit()` depuis le callback.
- La connexion RabbitMQ n'avait ni traitement de heartbeat régulier pendant
  l'attente BLE, ni délais réseau explicites, ni reconnexion indépendante.
  Un message GPS fictif daté de 2019 était envoyé au démarrage.
- Journalisation, redirection de stdout/stderr, ouverture du fichier et attente
  de 15 secondes étaient exécutées dès l'import. Des exceptions et données
  brutes étaient affichées. SIGTERM ne déclenchait pas de nettoyage explicite.

### Dépendances et déploiement disponibles

La passerelle fixe Bleak `3.0.2`, Paho `2.1.0`, victron-ble `0.9.3`, avec Python
3.12. Le GPS utilisait Python 3.9, `pip install bleak` sans version, et Pika
`1.1.0`. La version GPS réellement installée sur le Pi reste inconnue.
Le GPS passe à Python 3.12, Bleak `3.0.2` et Pika `1.3.2`, versions testées.
Les dépendances transitives et la base `python:3.12-slim` restent évolutives.

Le lancement principal documente `--net=host`, `--privileged`, montage D-Bus,
verrou et `--restart=always`. Aucun Compose ni commande complète de lancement
du **collecteur ESP32** n'est versionné. `k8s/004-deployment.yaml` concerne
l'application Java GPS, pas le collecteur BLE. On ne peut donc pas affirmer les
privilèges, montages, délais d'arrêt et politiques de redémarrage GPS réels.
Les exemples ci-dessous conservent les privilèges de la passerelle et proposent
le GPS avec D-Bus uniquement, sans privilège supplémentaire.

## 3. Concurrence et choix d'architecture

La [spécification officielle BlueZ Adapter1](https://github.com/bluez/bluez/blob/master/doc/org.bluez.Adapter.rst)
décrit des sessions de découverte partagées ; `StopDiscovery` libère la session
du client appelant. Les filtres des clients sont fusionnés. Il est donc faux de
déduire un conflit du seul fait que deux processus utilisent `hci0`.

La [documentation officielle BleakClient](https://bleak.readthedocs.io/en/latest/api/client.html)
signale la découverte implicite avec une adresse brute et recommande le
`BLEDevice` dans les usages complexes. Les deux applications l'utilisent
désormais. Les sources Bleak 3.0.2 locales ont aussi été examinées dans
`backends/bluezdbus/{scanner,manager,client}.py`.

| Option | Décision | Motif / condition de réévaluation |
| --- | --- | --- |
| A — Corrections locales | Retenue | Une boucle propriétaire par application, délais explicites, reconnexions limitées, aucun reset global. Corrige des défauts démontrés sans protocole supplémentaire |
| B — Coordination interprocessus | Non activée | Si des mesures reproduisent des échecs lors du chevauchement, concevoir un protocole de pause/acquittement des scanners et un verrou `flock` sur un même fichier hôte monté dans les deux conteneurs, avec délais et libération sur crash. Un verrou local asyncio ne suffit pas ; un verrou couvrant toute la connexion GPS affamerait la passerelle |
| C — Gestionnaire BLE central | Différée | Éviter un service, un contrat IPC et un point de panne supplémentaires tant que les mesures ne démontrent pas leur bénéfice. Pertinent si B finit par nécessiter un véritable ordonnanceur partagé |

La connexion GPS persistante coexiste avec les annonces et les lectures Inkbird.
Cela reste une hypothèse matérielle à valider. Ni A ni les tests simulés ne
prouvent que ce contrôleur supporte durablement toutes les opérations concurrentes.

## 4. Politique de récupération implémentée

| Événement | Action | Confirmation / limite |
| --- | --- | --- |
| Silence de la passerelle | Stop borné puis nouveau scanner ; intervalle 120, 240, 480, 960, 1800 s, puis plafond | Une annonce quelconque confirme la reprise et remet les délais à zéro ; aucun redémarrage de processus sur silence seul |
| Erreur explicite de démarrage | Nettoyage, attente 1, 2, 4, 8… s plafonnée à 60 s | Cinq erreurs non effacées par une annonce entraînent niveau 2 |
| Lecture Inkbird en échec | Déconnexion, tentative bornée puis prochaine période de cette sonde | Les autres mesures continuent ; pas de reset de contrôleur |
| ESP32 absent | Scan borné puis attente 60, 120, 240, 480, 900 s | Attente illimitée dans le temps, aucune sortie parce que le contact est coupé |
| GPS déconnecté | Nettoyage puis recherche avec backoff | Une notification valide confirme la reprise et réinitialise compteurs/délais |
| GPS connecté sans notification valide pendant 300 s | Déconnexion/reconnexion du seul client GPS | Le silence ne compte pas comme erreur D-Bus fatale |
| Timeout connexion ou abonnement GPS | Déconnexion même après succès partiel, puis backoff | Cinq erreurs opérationnelles sans notification valide entraînent niveau 2 |
| Démarrage scanner expiré ou nettoyage non confirmé | Aucun nouveau scanner/client dans ce processus ; niveau 2 | Fermeture des sockets D-Bus par la sortie du processus |
| Niveau 2 | Attente de 60 s, sortie non nulle, redémarrage par Docker | SIGTERM interrompt l'attente ; Docker reste nécessaire pour relancer |
| Coroutine ignorant l'annulation | Grâce de 2 s puis délai de 60 s et `os._exit(1)` | Dernier recours : pas de drainage asyncio infini ; sockets fermées par l'OS, tampons mémoire perdus |
| MQTT/RabbitMQ indisponible | Reconnexion indépendante et file bornée | Aucun redémarrage BLE motivé par un broker indisponible |

`runtime.py` est un petit helper identique dans les deux images ; le test de
coexistence vérifie cette égalité. Il ne coordonne pas les processus. Une boucle
Python/extension native totalement immobilisée ne peut pas exécuter cette garde ;
Docker peut la tuer lors d'un arrêt manuel, mais aucun watchdog matériel n'est
installé ici. Il n'existe aucun reboot automatique du Pi ni reset BlueZ depuis
les conteneurs. L'escalade contrôleur est absente par défaut (voir section 9).

## 5. Observabilité et compatibilité

La passerelle enrichit seulement son topic de diagnostic existant
`van/bluetooth/health` et écrit le même bilan en logs, par défaut toutes les
30 s. Il contient dernière annonce et âge monotone, dernière lecture GATT et
âge, état scanner, nombre de reprises, erreurs GATT, erreurs de démarrage,
silence suspect (`silent_or_unverified`) et niveau de récupération.
`running` décrit l'état applicatif, pas une preuve de réception.

Le sous-objet `mqtt` contient état réseau, topics en attente, échecs et dernier
succès **acquitté** par PUBACK. Les callbacks d'acquittement utilisent un verrou
de métriques distinct pour éviter l'inversion des verrous avec Paho.
Le dernier succès MQTT peut concerner un bilan de santé : vérifier aussi les
timestamps de chaque mesure attendue. `online` signifie connexion au broker.

Le GPS écrit `GPS health` en JSON : états scanner/connexion, dernière découverte
(tout périphérique), dernière notification valide et leurs âges, scans sans
cible, notifications invalides/silencieuses, erreurs opérationnelles,
récupérations, prochain délai, état RabbitMQ, dernière publication confirmée,
file et nombre de messages abandonnés. `last_gatt_read=null` est normal : il
utilise des notifications, pas des lectures de caractéristique.
Les timestamps sont UTC explicites ; les durées utilisent une horloge monotone.

Topics MQTT, noms/MAC, décodeurs Ruuvi/Victron/Inkbird et clés sont préservés.
Le contrat RabbitMQ conserve vhost `/`, exchange vide, routing key/queue durable
`gps`, `content_type=application/json`, les mêmes champs Java et vitesse en m/s.
Les confirmations et `mandatory=True` détectent les publications non routées.
Le message fictif de 2019 est supprimé ; seuls les relevés reçus sont envoyés.
Un JSON GPS invalide est compté et ignoré sans arrêter le processus ni afficher
les données brutes. Les erreurs sont journalisées par type, jamais par contenu.

Un thread unique possède les objets Pika ; les callbacks BLE n'effectuent plus
de réseau. Sa file mémoire conserve au maximum 128 messages, abandonne les plus
anciens en saturation et conserve un message non acquitté pour le réessayer.
Une reconnexion peut donc produire un doublon (acquittement perdu) et une panne
longue ou un redémarrage peut perdre des messages : aucun historique durable ni
exactly-once n'est promis. Les modes de livraison historiques ne sont pas changés
en messages persistants. Ne pas utiliser ces files comme archivage GPS complet.

Les paramètres communs sont `BLUETOOTH_ADAPTER=hci0`, `BLE_FAILURE_LIMIT=5`,
`BLE_CLEANUP_TIMEOUT_SECONDS=10`, `SCANNER_OPERATION_TIMEOUT_SECONDS=20`,
`BLE_PROCESS_RESTART_DELAY_SECONDS=60`, `HEALTH_INTERVAL_SECONDS=30`.
La passerelle ajoute `SCANNER_SILENCE_RETRY_MAX_SECONDS=1800` ; les autres
paramètres sont dans [README.md](README.md). Les paramètres GPS détaillés sont
dans [son README](../ch.bus.burry-gps/low-level-esp32/README.md).
L'ancien délai global GPS de 900 s avant arrêt est retiré ; il ne faut plus
l'utiliser pour interpréter les 900 s du nouveau plafond de reconnexion.

## 6. Tests reproductibles et validation locale

Depuis chaque répertoire (`ch.bus.bluetooth-mqtt`, puis
`ch.bus.burry-gps/low-level-esp32`) :

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-test.txt
python -m pytest -q
```

Sous Windows, remplacer l'activation par `.venv\Scripts\Activate.ps1`.
Pour le test de coexistence, les deux dépôts doivent être côte à côte ; sinon
ces deux tests sont explicitement sautés. Aucun test n'utilise un adaptateur.
Les scénarios couvrent silence pendant huit heures simulées, arrêt refusé ou
bloqué, `StartDiscovery`/`InProgress`, timeout D-Bus, lecture GATT bloquée,
déconnexion/abonnement partiels, absence ESP32 pendant huit heures simulées,
deux sessions applicatives, reprise confirmée, échec persistant, fin forcée,
SIGTERM et annulation sans tâche résiduelle, backoffs et contrats de publication.
Le scénario à deux clients simule le contrat de sessions BlueZ, pas le matériel.

Les résultats exécutés et constructions sont consignés dans
[BLE_VALIDATION.md](BLE_VALIDATION.md). La durée simulée n'est pas une endurance
radio de huit heures. La validation réelle ci-dessous reste indispensable.

## 7. Construction et bascule manuelles sur le Pi

Commandes à exécuter ultérieurement dans une fenêtre de maintenance, avec
accès local de secours. Aucun exemple de cette section n'a été exécuté sur le Pi.
Les chemins partent de la racine de la passerelle avec les dépôts côte à côte.
Si Compose/systemd gère les conteneurs, adapter la définition existante plutôt
que laisser l'orchestrateur recréer les anciens conteneurs pendant la bascule.

```sh
docker build --platform linux/arm64 -t ch.bus.bluetooth-mqtt:ble-reliability .
docker build --platform linux/arm64 -t ch.bus.burry-gps/low-level-esp32:ble-reliability \
  ../ch.bus.burry-gps/low-level-esp32

# Conserver les fichiers de secrets existants ; ne pas les remplacer par les exemples.
chmod 600 bluetooth.env gps.env

# Inventaire filtré : n'affiche ni variables d'environnement ni secrets.
docker inspect --format '{{.Name}} image={{.Image}} restart={{.HostConfig.RestartPolicy.Name}} privileged={{.HostConfig.Privileged}} network={{.HostConfig.NetworkMode}} mounts={{range .Mounts}}{{.Source}}:{{.Destination}} {{end}}' \
  bluetooth-mqtt low-level-esp32-gps
docker exec bluetooth-mqtt python -m pip show bleak paho-mqtt victron-ble dbus-fast
docker exec low-level-esp32-gps python -m pip show bleak pika dbus-fast

# Conserver les anciennes images, conteneurs, montages et politiques pour rollback.
docker update --restart=no bluetooth-mqtt low-level-esp32-gps
docker stop --time 120 bluetooth-mqtt low-level-esp32-gps
docker rename bluetooth-mqtt bluetooth-mqtt-pre-reliability
docker rename low-level-esp32-gps low-level-esp32-gps-pre-reliability

docker run -d --restart=unless-stopped --name bluetooth-mqtt \
  --net=host --privileged --stop-timeout 120 \
  --log-opt max-size=10m --log-opt max-file=3 \
  -v /var/run/dbus:/var/run/dbus \
  -v /run/bluetooth-mqtt:/run/bluetooth-mqtt \
  --env-file bluetooth.env ch.bus.bluetooth-mqtt:ble-reliability

docker run -d --restart=unless-stopped --name low-level-esp32-gps \
  --net=host --stop-timeout 120 \
  --log-opt max-size=10m --log-opt max-file=3 \
  -v /var/run/dbus:/var/run/dbus \
  --env-file gps.env ch.bus.burry-gps/low-level-esp32:ble-reliability
```

`gps.env` doit déjà contenir les quatre paramètres existants
`spring.rabbitmq.host`, `.port`, `.username`, `.password`.
Reporter aussi les montages de logs nécessaires, paramètres personnalisés et
règles D-Bus existantes. L'exemple n'ajoute aucun privilège au GPS : vérifier
l'autorisation D-Bus sur l'hôte, ne pas élargir les permissions pour masquer un
échec. Le montage du bus donne déjà des capacités sensibles au conteneur.
Les images restent versionnées par tag distinct et ne remplacent pas `latest`.

Les fichiers d'exemple ne contiennent aucun secret réel. Ne pas publier de
`docker inspect` intégral, d'environnement, de clés ni de captures HCI contenant
des positions GPS. Les logs Docker et fichier GPS ont une rotation bornée.

## 8. Validation réelle et rollback

Observer d'abord chaque service seul, puis ensemble avec GPS allumé, puis
ensemble avec GPS éteint pendant au moins huit heures. Ne pas conclure à une
amélioration sur le seul retour d'un message `scanner started`.

```sh
docker logs --timestamps --since 15m bluetooth-mqtt
docker logs --timestamps --since 15m low-level-esp32-gps
docker stats --no-stream bluetooth-mqtt low-level-esp32-gps
docker inspect --format '{{.Name}} restarts={{.RestartCount}} state={{.State.Status}} exit={{.State.ExitCode}}' \
  bluetooth-mqtt low-level-esp32-gps
```

Avec un client MQTT configuré localement pour l'authentification, souscrire sans
rejouer les anciens retained (`mosquitto_sub -R`) aux familles existantes et
`van/bluetooth/health`. Vérifier des timestamps nouveaux, chaque appareil
Victron, les quatre sondes, les valeurs GATT Inkbird et le DHT22 indépendant.
Le consommateur GPS existant doit recevoir les mêmes champs et unités ; ne pas
vider la queue `gps` en y attachant un consommateur de test concurrent en production.

Critères d'acceptation :

1. GPS absent : recherches espacées, aucune sortie liée à l'absence, mémoire
   bornée, publications Ruuvi/Victron/Inkbird maintenues.
2. GPS rétabli : reprise au plus tard après plafond de 900 s + scan + connexion,
   notification valide, puis publication RabbitMQ confirmée et délai réinitialisé.
3. Sonde absente trois cycles : seule sa disponibilité change ; autres mesures
   fraîches et pas d'accumulation de connexions.
4. Déconnexion GPS et arrêt propre de chaque conteneur : scanner/connexion
   libérés, aucun impact durable sur l'autre service, code de sortie 0 sur SIGTERM.
5. Coupure de broker **sur banc ou fenêtre de maintenance** : BLE continue,
   files bornées, reprise indépendante, compteurs de pertes/doublons compris.
6. Injecter les erreurs D-Bus sur banc avec les fakes ; un arrêt BlueZ réel
   perturbe les deux applications et n'est pas un test à lancer en voyage.
7. Surveiller 48–72 heures puis plusieurs semaines : âges de mesure, mémoire,
   reprises, échecs, connexions et redémarrages. Des logs de santé frais sans
   mesures fraîches ne suffisent pas.

En cas de récidive, relever avant une récupération : date UTC et heure locale,
logs des deux services, versions BlueZ/kernel/images, journal Bluetooth/kernel,
états `busctl --system list`, `bluetoothctl show`, `rfkill list` et si possible
capture HCI courte via `btmon` avec stockage protégé. Ne pas lancer une nouvelle
commande de scan concurrente pour « vérifier » le scanner. Une comparaison
GPS arrêté/allumé/absent, avec mêmes capteurs et conditions radio, est nécessaire
pour renforcer ou affaiblir l'hypothèse d'interaction.

Retour arrière (les nouveaux conteneurs sont eux aussi conservés) :

```sh
docker update --restart=no bluetooth-mqtt low-level-esp32-gps
docker stop --time 120 bluetooth-mqtt low-level-esp32-gps
docker rename bluetooth-mqtt bluetooth-mqtt-reliability-failed
docker rename low-level-esp32-gps low-level-esp32-gps-reliability-failed
docker rename bluetooth-mqtt-pre-reliability bluetooth-mqtt
docker rename low-level-esp32-gps-pre-reliability low-level-esp32-gps
# Restaurer ici les politiques initiales relevées : exemple si elles étaient always.
docker update --restart=always bluetooth-mqtt low-level-esp32-gps
docker start bluetooth-mqtt low-level-esp32-gps
```

Restaurer les définitions Compose/systemd si elles avaient été modifiées.
Aucun changement de schéma MQTT/RabbitMQ ni de firmware n'est à annuler.
Ne jamais faire tourner ancien et nouveau collecteur des mêmes mesures ensemble.

## 9. Récupération contrôleur externe, optionnelle et désactivée

Aucune automation, unité systemd ou commande privilégiée de récupération hôte
n'est installée. Les deux applications ne touchent ni `Powered`, ni rfkill, ni
BlueZ global. Une éventuelle récupération hôte exige une fenêtre de maintenance
et concerne **tous** les clients Bluetooth, y compris audio/claviers éventuels.

Procédure opérateur, seulement après recueil des preuves et sorties applicatives
inefficaces : acquérir sur l'hôte un verrou exclusif
`flock /run/ble-controller-recovery.lock`, arrêter les deux conteneurs et tout
autre client BLE, vérifier leurs états arrêtés et l'absence de relance par
Compose/systemd. Un simple verrou non respecté par les clients ne coordonne rien.
Dans cette fenêtre exclusive seulement, choisir soit une réactivation de `hci0`,
soit `sudo systemctl restart bluetooth`, attendre le retour du bus/contrôleur,
puis redémarrer les deux conteneurs et vérifier des annonces/notifications réelles.
Relâcher le verrou après validation, conserver les logs avant/après.
Ne jamais enchaîner une boucle de resets ; en cas d'échec, garder les preuves
et prévoir une intervention physique. Aucun reboot automatique du Raspberry.

## 10. Risques et hypothèses restant à vérifier

- L'état du contrôleur au moment de la panne n'est pas connu ; un défaut
  firmware/HCI ou électrique peut résister à toute récupération de processus.
- A ne supprime pas les chevauchements entre connexion GPS et découverte/GATT
  de la passerelle. L'absence de régression dans les fakes ne prédit pas la radio.
- Un environnement réellement silencieux et un scanner muet peuvent rester
  indiscernables. Le compromis choisi limite les reprises à 30 minutes au plafond.
- Les pauses GATT suspendent la réception de la passerelle, et les cinq échecs
  GPS peuvent être dus à une cible qui annonce mais refuse les connexions, sans
  panne du contrôleur. La sortie reste locale et temporisée.
- Redémarrage Docker requis pour le niveau 2 ; aucun daemon d'escalade matérielle
  n'est ajouté. Aucun healthcheck Docker ne traite un appareil absent comme panne.
- Tampons mémoire perdus à la sortie ; doublons RabbitMQ possibles ; conservation
  de la dernière mesure MQTT plutôt qu'historique complet.
- Pika, Python et Bleak GPS changent de version : vérifier protocoles, accès
  D-Bus et stabilité sur l'image ARM64 réelle. Transitives/base non figées par digest.
- Après un scan GPS, des callbacks de découverte cessent normalement même
  quand la connexion GATT est saine : l'âge de découverte seul n'est pas une alarme.
- Les snapshots de santé sont des observations ; ils ne sont ni des garanties
  de livraison métier ni un diagnostic matériel.

## Annexe — reboot à chaud, diagnostic séparé

L'arrêt Linux propre suivi d'un non-retour et d'un power-cycle réussi ne permet
pas de relier le reboot au BLE. Aucune correction système n'est faite ici.
Prévoir ultérieurement, en présence physique avec sauvegarde et console série :

- identifier modèle/révision du Pi, alimentation, convertisseur embarqué,
  câbles, chutes de tension et indicateurs historiques de sous-voltage ;
- relever versions kernel, firmware, EEPROM/bootloader et configuration de boot
  **sans mise à jour ni écriture** ; comparer ensuite aux notes officielles ;
- vérifier console série et journaux persistants pour situer l'arrêt, le reset,
  l'amorçage et la perte de réseau ; distinguer absence de boot et absence de réseau ;
- comparer redémarrage à chaud et démarrage à froid, puis tester séparément
  les périphériques USB/HAT/CP2102, stockage et éventuelles alimentations de retour ;
- examiner l'état et la politique du watchdog matériel, sans l'activer à distance ;
- ne mettre à jour firmware/EEPROM/OS ou changer la politique de watchdog
  qu'après validation explicite et procédure de récupération physique.
