# Compte rendu de la refonte Bluetooth / MQTT

Implémentation terminée dans les trois dépôts le 7 septembre 2026. Les tests
logiciels et les constructions Docker locales passent. Le déploiement et les
vérifications matérielles sur Raspberry Pi restent à effectuer.

## Architecture réalisée

`ch.bus.bluetooth-mqtt` possède un seul scanner Bleak. Un callback distribue les
annonces aux décodeurs Victron, Ruuvi, SensorBlue/ThermoBeacon et Inkbird.
Les décodeurs Victron sont appelés directement, sans scanner de `victron_ble`
et sans sous-processus. Les lectures GATT Inkbird se font séquentiellement,
scanner suspendu, à partir du `BLEDevice` déjà découvert. Chaque tentative
possède des délais de connexion/lecture et une déconnexion dans `finally`.

Le propriétaire coordonne les reprises, le watchdog et SIGTERM/SIGINT.
Les erreurs de démarrage sont réessayées avec backoff borné. Quand l'acquisition
ou le nettoyage est incertain, le processus quitte pour que Docker le relance
après fermeture de ses connexions DBus, sans créer un scanner concurrent.
Un verrou partagé empêche plusieurs instances de la passerelle de démarrer
avec les commandes de déploiement fournies.

MQTT se reconnecte indépendamment. La dernière valeur par topic est conservée
dans un tampon borné ; les lectures intermédiaires d'une coupure ne constituent
pas un historique garanti. Les messages sont retenus en QoS 1. Les identifiants
et clés ne sont jamais interpolés dans les erreurs de bibliothèque ; les clés
Victron et le mot de passe MQTT sont exclus des représentations de configuration.

## Fichiers par dépôt

### ch.bus.bluetooth-mqtt

Modifié : `README.md`.

Ajoutés :

- `.gitignore`, `.dockerignore`, `Dockerfile`, `bluetooth.env.example` ;
- `requirements.txt`, `requirements-test.txt`, `pytest.ini` ;
- `bluetooth_gateway/__init__.py`, `bluetooth_gateway/__main__.py` ;
- `bluetooth_gateway/config.py`, `bluetooth_gateway/service.py` ;
- `bluetooth_gateway/temperature.py`, `bluetooth_gateway/victron.py` ;
- `bluetooth_gateway/publishing.py` ;
- `tests/__init__.py`, `tests/conftest.py` ;
- `tests/test_decoding.py`, `tests/test_lifecycle.py`, `tests/test_mqtt.py` ;
- `tests/fixtures/temperature.json`, `tests/fixtures/victron.json`,
  `tests/fixtures/README.md` ;
- `MIGRATION.md`, `REFACTOR_REPORT.md`.

Aucun fichier source supprimé. Le fichier utilisateur `.project` n'a pas été modifié.
L'environnement virtuel local et les caches sont ignorés par Git.

### ch.bus.temperature-mqtt

Modifiés : `.gitignore`, `README.md`, `api/app.py`, `api/test_app.py`,
`temperature/app.py`, `temperature/test_app.py`, `temperature/Dockerfile`,
`temperature/README.md`.

Ajouté : `requirements-test.txt`.

Aucun fichier supprimé. Les fonctions BLE et leurs anciens tests ont été retirés
du collecteur et remplacés par ceux de la passerelle. `temperature/` conserve son
nom d'image et ne collecte que DHT22. L'API accepte facultativement la même liste
`TEMPERATURE_SENSORS` que la passerelle ; son comportement par défaut reste identique.

### ch.bus.victron-mqtt

Modifiés : `.gitignore`, `README.md`.

Ajoutés : `api/test_app.py`, `mppt/README.md`, `requirements-test.txt`.

Supprimés : `mppt/app.py`, `mppt/Dockerfile`, `mppt/test_app.py`.

Le code, les routes, les dépendances et le Dockerfile de l'API sont conservés.
Le répertoire `mppt/` contient désormais une note de migration, pas un collecteur.

## Dépendances et Docker

La passerelle ajoute et fixe `bleak==3.0.2`, `victron-ble==0.9.3` et
`paho-mqtt==2.1.0`. Les dépendances transitives installées incluent notamment
`dbus-fast` et `pycryptodome`. La version des anciens conteneurs Raspberry Pi
n'était pas disponible localement ; leur Dockerfile installait Victron sans
version fixe. La version 0.9.3 a été installée, inspectée et réellement utilisée
pour déchiffrer les fixtures des tests.

Les tests ajoutent pytest, pytest-asyncio dans la passerelle, et httpx/FastAPI
pour les vérifications HTTP. Les projets API conservent leurs dépendances de
production. Le collecteur DHT22 conserve Adafruit DHT, RPi.GPIO et libgpiod3,
fixe Paho à 2.1.0 et retire Bleak, bluetooth et bluez.

La suppression de l'image `mppt` retire son installation de victron-ble, Bleak,
bluetooth et bluez. Seule la commande de lancement de la passerelle demande
le mode privilégié et le montage DBus. Le DHT22 utilise un accès GPIO explicite ;
les API n'ont aucun périphérique hôte. Les exemples de lancement de scans
concurrents ont été retirés.

## Compatibilité

Préservés : les JSON et champs scalaires `van/victron/<appareil>[/<champ>]`,
les alias par modèle, les quatre identifiants de sondes BLE, leurs topics
`van/temperature/<sonde>[/<champ>]`, le DHT22 et les topics `availability`.
Les timestamps restent ISO 8601 ; pour les annonces, ils représentent désormais
l'observation plutôt que l'instant ultérieur de publication.

Les routes HTTP, statuts d'attente 503, identifiants par défaut et réponses restent
compatibles. L'historique température garde sa règle d'exclusion des paquets
retenus rejoués après reconnexion. L'API Victron garde son abonnement par défaut
historique `van/victron-mppt/smartsolar_pyleas` et ses six métriques SmartSolar.
La procédure montre explicitement l'abonnement `van/victron/smartsolar_pyleas`
pour la famille actuelle. Aucun nouvel endpoint HTTP n'a été ajouté.

Ajoutés : `van/bluetooth/status`, `van/bluetooth/health`,
`van/temperature/dht22/status`, `van/temperature/dht22/scan`.

Changements de diagnostic documentés : `van/temperature/scan` ne compte plus le
DHT22, qui a son bilan séparé ; `van/temperature/status` concerne le collecteur
BLE. Les anciens alias `van/victron/status` et `van/temperature/status` sont
actualisés à la connexion et à l'arrêt propre. Pour détecter un arrêt brutal,
utiliser le Last Will canonique `van/bluetooth/status`. Cela évite de prétendre
qu'un client MQTT peut disposer de plusieurs Last Will.

## Validation exécutée

Sur Python 3.11 / Windows, avec l'environnement virtuel local :

| Suite | Résultat |
| --- | --- |
| Passerelle : `python -m pytest -q` | 45 tests réussis |
| Température / DHT22 : `python -m pytest -q temperature` | 5 tests réussis |
| Température / API : `python -m pytest -q api` | 7 tests réussis |
| Victron / API : `python -m pytest -q api` | 4 tests réussis |
| Total | **61 tests réussis** |

Les tests couvrent les trois modèles Victron avec des trames chiffrées synthétiques,
plusieurs appareils avec des clés distinctes, Ruuvi formats 3/5, ThermoBeacon,
Inkbird 9/18 octets, correction GATT des anciennes valeurs BlueZ, absences,
connexion/lecture GATT en erreur ou timeout, déconnexion et annulation,
`InProgress`, adaptateur absent, reprise après arrêt silencieux, timeout de
démarrage avec propriété incertaine, absence de scanner concurrent, SIGTERM,
MQTT déconnecté/file pleine/reconnexion, Last Will et absence de secrets dans
les erreurs. Les tests HTTP exercent les routes et la réception des mesures MQTT.

Les suites HTTP émettent deux avertissements de dépréciation de Starlette/httpx
et AnyIO ; aucun échec. Les vérifications `git diff --check` passent dans les
trois dépôts.

Les quatre images ont été construites sur Docker Desktop Linux, architecture
amd64, Python 3.12 :

```text
ch.bus.bluetooth-mqtt:refactor-test
ch.bus.temperature-mqtt/temperature:refactor-test
ch.bus.temperature-mqtt/api:refactor-test
ch.bus.victron-mqtt/api:refactor-test
```

Des exécutions isolées sans réseau confirment l'import du DHT22 et des deux API,
les réponses d'attente 503 et l'absence de Bleak/victron_ble dans ces images.
Une exécution réelle de la passerelle sans DBus ni broker reste active, journalise
l'indisponibilité et s'arrête sur SIGTERM avec le code 0. Le conteneur de test
a été supprimé. Aucun conteneur Raspberry Pi n'a été modifié ou déployé.

## Vérification Raspberry Pi et limites restantes

Les commandes complètes de construction, bascule, observation `mosquitto_sub`,
contrôle HTTP, inspection des privilèges et retour arrière sont dans
[MIGRATION.md](MIGRATION.md).

À valider sur le matériel : décodage des annonces réelles avec les clés locales,
portée et comportement BlueZ/contrôleur, lectures GATT et déconnexions effectives,
accès DHT22 avec le périphérique GPIO approprié, architecture ARM et stabilité
sur 24 à 48 heures. Les tests locaux n'établissent pas ces résultats matériels.

Les pauses GATT interrompent temporairement toutes les annonces, y compris
Victron ; ajuster délais/tentatives selon le matériel. Le watchdog ne peut pas
distinguer un environnement entièrement silencieux d'un scanner arrêté et
reprend alors périodiquement. Les alias Victron par modèle restent partagés si
plusieurs appareils du même modèle existent, conformément au comportement
historique. Les noms MQTT peuvent être figés avec `VICTRON_NAMES`.
