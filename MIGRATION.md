# Migration Raspberry Pi

Exécuter ces commandes sur le Raspberry Pi, avec les trois dépôts côte à côte.
Les exemples partent de la racine de `ch.bus.bluetooth-mqtt`. Adapter les noms
si les conteneurs existants portent d'autres noms. Aucun déploiement Raspberry
Pi n'est réalisé par les tests locaux.

## 1. Préparer les images et la configuration

```sh
docker build -t ch.bus.bluetooth-mqtt:latest .
docker build -t ch.bus.temperature-mqtt/temperature:latest ../ch.bus.temperature-mqtt/temperature
docker build -t ch.bus.temperature-mqtt/api:latest ../ch.bus.temperature-mqtt/api
docker build -t ch.bus.victron-mqtt/api:latest ../ch.bus.victron-mqtt/api
cp bluetooth.env.example bluetooth.env
chmod 600 bluetooth.env
```

Renseigner `bluetooth.env` localement avec les identifiants MQTT et les entrées
`VICTRON_DEVICES` existantes. Activer Instant Readout dans VictronConnect et
reprendre les noms actuels avec `VICTRON_NAMES` si nécessaire. Aucune vraie clé
ne doit être placée dans Git. Ne pas afficher les variables d'environnement des
conteneurs dans les logs ou dans un compte rendu.

Créer aussi `mqtt.env` localement, contenant seulement les quatre paramètres
`MQTT_HOST`, `MQTT_PORT`, `MQTT_USERNAME`, `MQTT_PASSWORD` pour les API et DHT22.
Si l'ancienne racine Victron était `van/victron-mppt`, choisir cette valeur pour
`MQTT_VICTRON_BASE_TOPIC` et garder l'abonnement API existant ; sinon utiliser
`van/victron` et configurer l'API explicitement.

Les exemples ci-dessous utilisent `van/victron`, `van/temperature`, le port
8013 pour la température et 8012 pour Victron. Les deux API avaient le même port
par défaut : garder les ports déjà exposés dans votre déploiement, ou choisir
deux ports distincts lorsqu'elles tournent sur le même hôte.

## 2. Arrêter et conserver les anciens collecteurs

```sh
docker ps --format 'table {{.Names}}	{{.Image}}	{{.Status}}'
docker update --restart=no victron-ble temperature-mqtt
docker stop --time 45 victron-ble temperature-mqtt
docker rename victron-ble victron-ble-pre-bluetooth
docker rename temperature-mqtt temperature-mqtt-pre-bluetooth
```

Les anciens conteneurs et leurs configurations sont conservés pour revenir en
arrière. Désactiver aussi tout ancien service systemd/Compose/collecteur externe
qui pourrait les relancer. Ne pas lancer de commande de scan manuelle en parallèle.

## 3. Démarrer la passerelle seule

```sh
docker run -d --restart=always --name bluetooth-mqtt \
  --net=host --privileged --stop-timeout 45 \
  -v /var/run/dbus:/var/run/dbus \
  -v /run/bluetooth-mqtt:/run/bluetooth-mqtt \
  --env-file bluetooth.env ch.bus.bluetooth-mqtt:latest

docker logs --tail 100 bluetooth-mqtt
```

Attendre `BLE scanner started: hci0`, puis les annonces des appareils configurés
et les lectures GATT des deux Inkbird. Une sonde absente doit seulement être
signalée hors ligne après le nombre de cycles configuré.

## 4. Vérifier MQTT avant de remplacer les autres services

Avec `MQTT_USERNAME` et `MQTT_PASSWORD` définis dans le terminal local, observer
chaque famille (Ctrl+C pour terminer chaque abonnement) :

```sh
mosquitto_sub -h 127.0.0.1 -u "$MQTT_USERNAME" -P "$MQTT_PASSWORD" \
  -t 'van/bluetooth/#' -v
mosquitto_sub -h 127.0.0.1 -u "$MQTT_USERNAME" -P "$MQTT_PASSWORD" \
  -t 'van/victron/#' -v
mosquitto_sub -h 127.0.0.1 -u "$MQTT_USERNAME" -P "$MQTT_PASSWORD" \
  -t 'van/temperature/#' -v
```

Vérifier des **timestamps nouveaux**, pas uniquement des anciens paquets retenus.
On peut ajouter `-R` à `mosquitto_sub` pour ignorer les valeurs rejouées à la
connexion. Vérifier SmartSolar, Phoenix et BatteryProtect, ainsi que les quatre
sondes BLE ; pour Inkbird, comparer une variation réelle avec les paquets GATT.
Les champs Victron scalaires sont publiés dynamiquement, sans filtre par modèle.

## 5. Démarrer DHT22 et les API refactorisées

Le DHT22 reste sur GPIO 4. L'accès à `/dev/gpiomem` remplace le privilège global
sur les Raspberry Pi compatibles avec le backend RPi.GPIO existant.

```sh
docker run -d --restart=always --name temperature-mqtt \
  --net=host --device /dev/gpiomem:/dev/gpiomem \
  --stop-timeout 15 --env-file mqtt.env \
  -e MQTT_BASE_TOPIC=van/temperature \
  ch.bus.temperature-mqtt/temperature:latest
```

Pour les API déjà présentes, arrêter et renommer leurs anciens conteneurs avant
le lancement ci-dessous, comme pour les collecteurs. Garder leurs paramètres
MQTT, leur port HTTP et les paramètres de sondes personnalisés.

```sh
# Seulement si ces conteneurs existent déjà :
docker update --restart=no temperature-api victron-metrics-api
docker stop temperature-api victron-metrics-api
docker rename temperature-api temperature-api-pre-bluetooth
docker rename victron-metrics-api victron-metrics-api-pre-bluetooth

docker run -d --restart=always --name temperature-api \
  --net=host --env-file mqtt.env \
  -e MQTT_BASE_TOPIC=van/temperature -e API_PORT=8013 \
  ch.bus.temperature-mqtt/api:latest

docker run -d --restart=always --name victron-metrics-api \
  --net=host --env-file mqtt.env \
  -e MQTT_TOPIC=van/victron/smartsolar_pyleas -e API_PORT=8012 \
  ch.bus.victron-mqtt/api:latest
```

L'historique température reste en mémoire et repart à zéro au redémarrage de
l'API, comme auparavant. Transmettre `TEMPERATURE_SENSORS` à l'API si la
configuration BLE par défaut a été remplacée. Elle ajoute le DHT22 automatiquement.

## 6. Vérifier HTTP et l'absence d'accès Bluetooth ailleurs

```sh
curl --fail http://127.0.0.1:8013/api/health
curl --fail http://127.0.0.1:8013/api/sensors
curl --fail http://127.0.0.1:8013/api/sensors/dht22
curl --fail http://127.0.0.1:8013/api/history
curl --fail http://127.0.0.1:8012/api/health
curl --fail http://127.0.0.1:8012/api/metrics

docker inspect --format '{{.Name}} privileged={{.HostConfig.Privileged}} mounts={{range .Mounts}}{{.Source}}:{{.Destination}} {{end}} devices={{range .HostConfig.Devices}}{{.PathOnHost}} {{end}}' \
  bluetooth-mqtt temperature-mqtt temperature-api victron-metrics-api

docker top bluetooth-mqtt
docker top temperature-mqtt
docker top temperature-api
docker top victron-metrics-api
```

Seul `bluetooth-mqtt` doit être privilégié et monter DBus. Le DHT22 ne reçoit
que son périphérique GPIO. Les API ne reçoivent aucun périphérique hôte.
Sur l'hôte, `busctl --system list` peut compléter la vérification des clients
DBus ; ne pas afficher `docker inspect` sans filtre, qui exposerait les secrets.
La vérification des processus/montages prouve la configuration du déploiement ;
elle ne remplace pas l'observation radio sur la durée.

## 7. Vérifier la récupération en conditions réelles

- Mettre une Inkbird hors portée pendant trois cycles : les autres mesures
  doivent continuer et le service ne doit pas accumuler de connexions.
- Couper puis rétablir le broker pendant une fenêtre de maintenance : les
  dernières valeurs et les statuts doivent revenir automatiquement.
- Exécuter `docker stop --time 45 bluetooth-mqtt`, vérifier le statut `offline`,
  puis `docker start bluetooth-mqtt` et la reprise des topics.
- Observer sur 24 à 48 heures les timestamps, `scanner_restarts`, les messages
  GATT, l'utilisation mémoire et le nombre de processus. Une augmentation
  permanente des reprises nécessite d'examiner le contrôleur et BlueZ de l'hôte.

Ces essais radio, GPIO et de durée restent à effectuer sur le Raspberry Pi.
Le watchdog se base sur l'absence de toute annonce : il peut reprendre le scanner
si l'environnement est entièrement silencieux. Les pauses GATT sont temporaires
mais suspendent aussi la réception Victron pendant leur durée.

Un timeout de démarrage sans réponse BlueZ, un arrêt scanner non confirmé ou
une déconnexion GATT non confirmée entraîne une sortie non nulle. Docker
relance le processus après fermeture de ses connexions DBus. Aucun second
scanner n'est créé pour masquer cet état incertain.

## Retour arrière

Arrêter la passerelle **avant** de réactiver un ancien collecteur. Les conteneurs
anciens renommés conservent leurs images et configurations.

```sh
docker update --restart=no bluetooth-mqtt
docker stop --time 45 bluetooth-mqtt
docker stop temperature-mqtt
docker start temperature-mqtt-pre-bluetooth
# Pour un diagnostic Victron séparé, arrêter de nouveau le collecteur BLE
# température avant de démarrer victron-ble-pre-bluetooth.
```

Le retour aux deux anciens collecteurs simultanément réintroduit le conflit
BlueZ initial. Ne les utiliser que séparément pendant le diagnostic. Restaurer
les anciennes API si nécessaire et rétablir la politique de redémarrage choisie
uniquement pour les conteneurs conservés.
