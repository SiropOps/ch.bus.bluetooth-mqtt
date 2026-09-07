# ch.bus.bluetooth-mqtt

Passerelle BLE vers MQTT pour Raspberry Pi. Ce service possède seul le contrôleur
Bluetooth et la découverte BlueZ. Il ne fournit aucune API HTTP.

```text
BlueZ / hci0 -> un BleakScanner -> dispatcher
                                  |-> décodeurs Victron Instant Readout
                                  |-> décodeurs Ruuvi / ThermoBeacon / Inkbird
                                  |-> lectures Inkbird GATT coordonnées
                                              |
                                              v
                                             MQTT
                                     /                   \
                         temperature-mqtt/api       victron-mqtt/api
DHT22 GPIO -> temperature-mqtt/temperature -> MQTT
```

La boucle propriétaire sérialise démarrage, arrêt, reprise et GATT. Les callbacks
ne créent aucune tâche. Une lecture GATT utilise le `BLEDevice` déjà découvert,
ce qui évite la découverte implicite que Bleak ferait avec une adresse seule.
Le scanner s'arrête avant GATT ; chaque tentative a un délai de connexion, un
délai de lecture et une déconnexion dans `finally`. Il reprend après les lectures.
Les Inkbird de 9 octets sont relues via FFF2 ; les modèles de 18 octets peuvent
être lus par annonce. Les deux Inkbird historiques conservent le mode GATT.

Victron utilise directement `detect_device_type(data)(key).parse(data)` de
`victron-ble==0.9.3`, inspecté et testé avec la version installée. La conversion
reprend `DeviceDataEncoder` : méthodes `get_*`, enums en minuscules, valeurs
`None` omises. Aucun scanner ni sous-processus de cette bibliothèque n'est lancé.
Sources : [API amont](https://github.com/keshavdv/victron-ble),
[version utilisée](https://pypi.org/project/victron-ble/0.9.3/).

## Construction et lancement

```sh
docker build -t ch.bus.bluetooth-mqtt:latest .
cp bluetooth.env.example bluetooth.env
# Renseigner localement les identifiants MQTT et les clés Victron.
chmod 600 bluetooth.env
```

Le Dockerfile installe temporairement `gcc` et `libc6-dev` pour compiler les
dépendances natives lorsqu'aucun paquet binaire n'est disponible sur Raspberry
Pi (notamment `pycryptodome` et `dbus-fast`). Ces outils sont retirés après
l'installation. La construction peut donc être plus longue sur ARM.

**Arrêter d'abord les anciens collecteurs BLE.** La procédure complète, avec
vérification et retour arrière, est dans [MIGRATION.md](MIGRATION.md).

```sh
docker run -d --restart=always --name bluetooth-mqtt \
  --net=host --privileged \
  --stop-timeout 45 \
  -v /var/run/dbus:/var/run/dbus \
  -v /run/bluetooth-mqtt:/run/bluetooth-mqtt \
  --env-file bluetooth.env \
  ch.bus.bluetooth-mqtt:latest
```

Le verrou partagé dans `/run/bluetooth-mqtt` empêche deux instances de cette
passerelle de démarrer ensemble. Tous les lancements doivent partager ce même
répertoire. Les collecteurs d'autres applications doivent être désactivés
séparément. BlueZ reste le service de l'hôte ; aucun démon BlueZ supplémentaire
ni outil `bluetoothctl scan` n'est lancé dans le conteneur.

## Configuration

Les racines MQTT distinctes remplacent le `MQTT_BASE_TOPIC` des anciens
collecteurs. Copier sa valeur dans la variable de famille correspondante.
Ne pas transmettre les clés Victron aux API ni au conteneur DHT22.

| Variable | Défaut | Utilisation |
| --- | --- | --- |
| `MQTT_HOST` / `MQTT_PORT` | `127.0.0.1` / `1883` | Broker existant |
| `MQTT_USERNAME` / `MQTT_PASSWORD` | vide | Authentification |
| `VICTRON_DEVICES` | vide | `MAC@CLE_HEXA_32_CARACTERES,MAC@CLE,...` |
| `VICTRON_NAMES` | `{}` | JSON facultatif MAC vers nom MQTT historique |
| `TEMPERATURE_SENSORS` | quatre sondes historiques | Tableau JSON ; `[]` désactive les sondes BLE |
| `MQTT_VICTRON_BASE_TOPIC` | `van/victron` | Racine Victron, configurable aussi avec l'ancienne racine |
| `MQTT_TEMPERATURE_BASE_TOPIC` | `van/temperature` | Racine température BLE |
| `MQTT_STATUS_TOPIC` | `van/bluetooth/status` | Statut retenu et Last Will |
| `MQTT_HEALTH_TOPIC` | `van/bluetooth/health` | Santé du scanner |
| `BLUETOOTH_ADAPTER` | `hci0` | Contrôleur BlueZ |
| `VICTRON_READ_INTERVAL_SECONDS` | `30` | Intervalle minimal par appareil ; premier relevé immédiat |
| `TEMPERATURE_READ_INTERVAL_SECONDS` | `300` | Période des cycles BLE, après traitement du cycle précédent |
| `SCAN_TIMEOUT_SECONDS` | `45` | Attente avant le premier cycle température |
| `MISSED_CYCLES_BEFORE_OFFLINE` | `3` | Cycles température manqués avant `offline` |
| `INKBIRD_GATT_TIMEOUT_SECONDS` | `20` | Délai de connexion GATT |
| `GATT_READ_TIMEOUT_SECONDS` | `10` | Délai de lecture FFF2 |
| `GATT_ATTEMPTS` | `2` | Tentatives par cycle, maximum 5 |
| `BLE_CLEANUP_TIMEOUT_SECONDS` | `10` | Délai d'arrêt scanner / déconnexion GATT |
| `SCANNER_OPERATION_TIMEOUT_SECONDS` | `20` | Délai de démarrage scanner |
| `SCANNER_IDLE_TIMEOUT_SECONDS` | `120` | Reprise si aucune annonce ne parvient au scanner |
| `BLUETOOTH_RETRY_MAX_SECONDS` | `60` | Plafond du délai exponentiel après erreur |
| `HEALTH_INTERVAL_SECONDS` | `30` | Période de publication de la santé |
| `BLUETOOTH_OWNER_LOCK` | `/run/bluetooth-mqtt/owner.lock` | Verrou partagé entre instances |

Exemple de configuration facultative des noms (une seule ligne dans le fichier
Docker `--env-file`, sans guillemets englobant la valeur) :

```text
VICTRON_NAMES={"AA:BB:CC:DD:EE:01":"SmartSolar Pyleas","AA:BB:CC:DD:EE:02":"VE.Direct Pyleas","AA:BB:CC:DD:EE:03":"BatteryProtec Pyleas"}
```

Les MAC doivent correspondre à `VICTRON_DEVICES`. Sans cette configuration,
le nom BLE est utilisé, avec repli sur la MAC lorsqu'aucun nom n'est disponible.
Configurer les noms évite un changement de topic si un appareil est renommé ou
si sa première annonce ne comporte pas son nom.

La configuration par défaut des sondes est équivalente à :

```json
[
  {"name":"Ça pique","address":"E3:EE:E4:14:FA:B0","protocol":"ruuvi"},
  {"name":"Avalanche Toit","address":"9D:88:00:00:02:2C","protocol":"sensorblue"},
  {"name":"Fruit Storage","address":"49:22:11:08:18:64","protocol":"inkbird","gatt":"always"},
  {"name":"Tête used","address":"49:22:09:05:14:A1","protocol":"inkbird","gatt":"always"}
]
```

`gatt` accepte `auto` (défaut), `always` ou `never`. Conserver `always` pour les
deux sondes historiques : leurs annonces de 9 octets peuvent contenir une
ancienne température accumulée par BlueZ. Un absent est réessayé au prochain
cycle, sans connexion tentée en l'absence d'une annonce exploitable.
Transmettre le même `TEMPERATURE_SENSORS` à l'API température si les sondes changent.

## Compatibilité MQTT

Toutes les publications sont retenues, en QoS 1. Les payloads et les topics de
mesures sont conservés, y compris les champs scalaires et les alias de modèles :

```text
van/victron/smartsolar_pyleas[/<champ>]
van/victron/ve_direct_pyleas[/<champ>]
van/victron/batteryprotec_pyleas[/<champ>]
van/victron/<nom_du_modele>
van/temperature/ca_pique[/<champ>]
van/temperature/avalanche_toit[/<champ>]
van/temperature/fruit_storage[/<champ>]
van/temperature/tete_used[/<champ>]
van/temperature/<sonde>/availability
```

Les alias Victron par modèle restent communs aux appareils d'un même modèle,
comme auparavant ; utiliser le topic par appareil pour les distinguer.
Le DHT22 reste publié par le projet température sous `van/temperature/dht22`.
Les API et leurs réponses HTTP sont conservées.

Les diagnostics évoluent pour distinguer leurs propriétaires :

| Topic | Propriétaire / sens |
| --- | --- |
| `van/bluetooth/status` | `online` si la passerelle est connectée à MQTT, `offline` au Last Will / arrêt |
| `van/bluetooth/health` | état scanner, adaptateur, appareils configurés vus, dernière annonce, GATT actif, reprises |
| `van/victron/status` | alias de statut conservé, mis à jour à la connexion et à l'arrêt propre |
| `van/temperature/status` | alias conservé pour le collecteur BLE uniquement |
| `van/temperature/scan` | même schéma, désormais limité aux sondes BLE |
| `van/temperature/dht22/status` | nouveau statut et Last Will du collecteur GPIO |
| `van/temperature/dht22/scan` | nouveau bilan réservé au DHT22 |

MQTT ne fournit qu'un Last Will par client : **utiliser `van/bluetooth/status`
pour détecter un arrêt brutal**. Les deux anciens alias peuvent rester `online`
après un crash jusqu'à la reconnexion. `online` ne signifie pas que les appareils
ont répondu : consulter `health`, `availability` et les timestamps de mesure.

## Pannes et arrêt

Une erreur de démarrage, DBus ou `InProgress` entraîne nettoyage puis reprise
avec délai exponentiel plafonné. Le watchdog détecte également un arrêt silencieux
par absence d'annonces. Un environnement radio totalement vide provoque donc
une reprise périodique ; ajuster son délai si nécessaire.

Si le démarrage expire sans réponse BlueZ, ou si l'arrêt du scanner ou la
déconnexion GATT échoue, l'état Bluetooth est
incertain. La passerelle quitte avec un code non nul ; la politique Docker la
relance et la fin du processus ferme ses connexions DBus. Elle ne tente pas de
créer un second scanner dans le même processus. Aucun redémarrage de BlueZ par
`systemctl` n'est effectué. SIGTERM/SIGINT annulent l'opération courante, attendent
le nettoyage et ferment MQTT.

MQTT reconnecte indépendamment du Bluetooth. Un tampon conserve la dernière
valeur par topic (4096 topics maximum), avec une file Paho bornée à 1000 messages.
La reprise n'est pas un historique complet : les mesures intermédiaires d'une
coupure peuvent être remplacées par les plus récentes.

## Tests sans matériel

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-test.txt
python -m pytest -q
```

Les fixtures Victron chiffrées utilisent exclusivement une clé de test publique.
Les tests couvrent décodage, JSON/scalaires, plusieurs appareils, absence, GATT,
annulation, reprise scanner, MQTT et absence de secrets dans les logs.
La validation réelle BlueZ, GPIO et radio est décrite dans [MIGRATION.md](MIGRATION.md).
