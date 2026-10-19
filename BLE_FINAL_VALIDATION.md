# Validation finale de l'isolation BLE / RabbitMQ — 19 octobre 2026

## Verdict

**GO pour un déploiement contrôlé sur `hiva-oa`.** Les vérifications logicielles
sont satisfaisantes après la correction RabbitMQ décrite ci-dessous. Ce GO
ne démontre pas encore la coexistence radio réelle sur le contrôleur partagé.

Audit local des branches `codex/ble-reliability`, depuis les commits
`68fdc49` de la passerelle et `21d66b7` du GPS. Aucun accès à
`hiva-oa`, déploiement, push, changement de secrets ou modification des autres
services du van. Les simulations n'utilisent aucun adaptateur réel.

## 1. RabbitMQ et boucle BLE GPS

Le chemin vérifié est `ESP32 → start_notify → handle_notify → deque → worker
RabbitMQ`. Le callback décode le JSON, conserve le contrat GPS existant et
ajoute un message à la file. Les opérations Pika, reconnexions et confirmations
s'exécutent exclusivement dans le thread `rabbit-publisher`.

Le verrou de file protège uniquement des opérations mémoire courtes ; aucun
appel réseau, sommeil, attente de confirmation, fermeture ou journalisation
n'est effectué sous ce verrou. Le worker consulte la tête puis libère le
verrou avant de publier. En saturation, le callback retire immédiatement
l'ancienne tête et incrémente `dropped`. La file reste bornée à **128** positions.
Une publication en cours peut aussi retenir la référence du message déjà
sorti de la file pendant une saturation ; il n'existe aucun autre backlog.

Les abandons figurent dans `rabbitmq.dropped` des bilans JSON `GPS health`,
émis périodiquement (30 s par défaut) et à la fin du service. Il n'y a pas de
log synchrone par perte dans le callback. Le scénario de huit heures reçoit
**28 800 notifications à 1 Hz**, garde les 128 dernières positions et compte
**28 672 abandons**, avec bilans de pertes effectivement journalisés.
Il ne démarre qu'un scanner et une connexion GPS ; aucun timeout de notification,
échec BLE ou niveau de récupération n'est déclenché par RabbitMQ.

### Défaut démontré et correction nécessaire

Les paramètres antérieurs bornaient l'établissement TCP/AMQP et un broker
déclarant `Connection.Blocked`, mais **pas toutes les attentes de réponse**.
Le vrai Pika 1.3.2 reste dans `basic_publish` si le broker continue ses
heartbeats sans confirmer. Un serveur AMQP simulé sur loopback a reproduit
ce défaut avant correction. Cela immobilisait le worker, sans bloquer le BLE,
et empêchait la reconnexion RabbitMQ dans ce cas précis.

`low-level-esp32/publishing.py` ajoute `DeadlineConnection`, petite adaptation
de `pika.BlockingConnection`. Chaque `_flush_output` a une échéance absolue de
**10 s** ; le minuteur de la boucle I/O Pika termine le transport via le chemin
de nettoyage de Pika à expiration. Le signal d'arrêt est vérifié au plus toutes
les **0,2 s** pendant ces attentes. Les confirmations qui réussissent annulent
leur minuteur. L'ouverture de canal, la déclaration, `Confirm.Select`, l'attente
d'ACK et les deux fermetures sont testées avec le vrai Pika et un pair qui
continue à envoyer ses heartbeats.

Cette correction utilise `_flush_output` et `_impl._terminate_stream` internes
à **Pika épinglé à 1.3.2**. Son minuteur I/O utilise `ioloop.call_later` : le
`BlockingConnection.call_later` public diffère ses callbacks pendant une RPC
bloquante et ne fournit donc pas cette garantie. Lors d'une mise à jour Pika,
réexécuter ces tests et réexaminer ces points internes.

| Attente | Borne / comportement |
| --- | --- |
| Socket / établissement de pile | 5 s / 10 s ; une tentative par connexion |
| Broker déclarant la connexion bloquée | 5 s |
| RPC AMQP / confirmation / fermeture | 10 s par attente, indépendant des heartbeats |
| Reconnexion | 1, 2, 4, 8, 16, 32 puis 60 s maximum ; attente interrompue à l'arrêt |
| Arrêt du worker | `join` borné à 15 s, thread daemon ; sortie du processus possible même si une dépendance reste bloquée |

Heartbeat : 30 s configurées. Les échéances supposent que l'ordonnanceur et
Python exécutent normalement le thread. Ce ne sont pas des garanties temps réel.
L'établissement initial conserve les bornes Pika existantes ; le contrôle
d'arrêt de 0,2 s concerne les attentes d'une connexion déjà créée.

Timeouts, NACK, retours non routables et exceptions de publication sont capturés
dans le worker : fermeture locale, backoff puis reconnexion. Une confirmation
réussie seule met à jour `last_publish_success`. Le message en attente est
réessayé s'il n'a pas été évincé par la politique des 128 dernières positions.
Une confirmation perdue peut produire un doublon. La perte pendant panne ou
arrêt est acceptée ; aucun SQLite, spool ou stockage persistant n'est ajouté.

## 2. Isolation applicative entre GPS et passerelle

Le GPS n'appelle plus `BleakScanner.discover()` : `find_device()` possède
explicitement un scanner temporaire, bornant son démarrage et son arrêt.
Il arrête sa session avant sa connexion GATT et transmet le `BLEDevice`
découvert au client. Inkbird utilise aussi le `BLEDevice`, évitant une recherche
implicite supplémentaire depuis une adresse brute. Le chemin D-Bus du GPS
provient de la découverte sur l'adaptateur explicite `hci0`.

Le GPS absent attend indéfiniment avec des pauses de 60, 120, 240, 480 puis
900 s maximum entre tentatives. Huit heures d'absence ne constituent pas une
erreur fatale. La nouvelle simulation exerce les vrais cycles applicatifs
start/stop du scanner, en plus de l'ancien test de backoff.

Chaque service possède ses objets, sa boucle et son contexte D-Bus. Aucun
verrou applicatif n'est partagé entre les conteneurs ; `owner.lock` protège
seulement plusieurs instances de la passerelle. Aucun accès RabbitMQ ne
conditionne un scanner, une connexion GATT ou une reprise BLE.

Les sessions de découverte BlueZ sont partagées : `StopDiscovery` libère la
session du client appelant, et les filtres sont fusionnés. Cela ne signifie
pas que l'activité radio est strictement indépendante. La passerelle suspend
sa propre découverte pour Inkbird ; une session de découverte GPS peut encore
être active au même instant. Une connexion GPS peut également chevaucher une
lecture Inkbird. Les simulations vérifient que le code accepte ces chevauchements
sans verrou global, annulation croisée ou remise à zéro du contrôleur.

### Nettoyage et sorties

Les clients GATT sont déconnectés dans `finally`, y compris après connexion
partielle, timeout, échec d'abonnement ou annulation. Les tests Linux ajoutés
emploient les **vrais backends BlueZ Bleak 3.0.2** avec un bus simulé : ils
vérifient les callbacks, watchers, verrous et déconnexions du bus après erreur,
timeout ou annulation, sans contacter BlueZ.

Une réponse de démarrage ou d'arrêt perdue peut laisser la propriété d'une
session incertaine. Le code ne prétend pas l'avoir libérée : il interdit une
nouvelle acquisition dans le processus, puis sort après le délai de récupération.
La fermeture des sockets du processus permet alors à BlueZ de libérer ses
ressources côté client. La simulation ne prouve pas la réaction d'un vrai
daemon ou contrôleur en panne. Une coroutine D-Bus ignorant l'annulation reste
traitée par la garde existante : grâce de 2 s, délai de récupération puis
`os._exit(1)` en dernier recours, plutôt qu'un drainage asyncio infini.

SIGTERM/SIGINT annulent le propriétaire BLE et attendent son nettoyage. Docker
peut relancer un processus en erreur selon sa configuration ; cela ne constitue
pas un redémarrage BlueZ. L'arrêt/redémarrage simulé du GPS conserve la session
et la réception de la passerelle. Il s'agit d'une simulation des propriétaires,
pas d'une reproduction de Docker, du noyau ou des signaux D-Bus de production.

L'inspection des sources exécutables et Dockerfiles des deux applications ne
trouve aucun reset de `hci0`, `rfkill`, `hciconfig`, `btmgmt`, redémarrage BlueZ
ou reboot du Raspberry. Aucun changement de code BLE n'a été nécessaire ici.

## 3. Tests et résultats

Résultats finaux, sans broker ni adaptateur réels :

| Environnement | Passerelle | GPS | Total réussi |
| --- | --- | --- | --- |
| Windows, Python 3.11.2 | 65 réussis, 13 sautés (backend Linux) | 51 réussis | 116 |
| Docker Linux amd64, Python 3.12 | 78 réussis | 51 réussis | **129** |

**36 cas supplémentaires** par rapport aux 93 tests initiaux : 17 passerelle,
19 GPS. Aucun test sauté sous Linux ; les deux dépôts sont présents côte à
côte. Les sources sont montées en lecture seule dans le conteneur local,
sans montage D-Bus, périphérique Bluetooth ni accès à `hiva-oa`. Le pair AMQP
est un serveur loopback de test, qui exerce le vrai Pika et des frames AMQP.

| Demande | Vérification automatisée |
| --- | --- |
| 1. RabbitMQ indisponible huit heures | `test_isolation.py` : worker réel, backoffs virtuels, callbacks BLE et bilans de santé actifs |
| 2. File saturée | 28 800 entrées, 128 dernières conservées, compteur exact et journal de pertes ; saturation concurrente avec publication bloquée |
| 3. Publication bloquée/expirée | `test_amqp_deadlines.py` : ACK absent malgré heartbeats, RPC d'ouverture/déclaration/confirmation/fermeture sans réponse, sockets fermées |
| 4. Reconnexion RabbitMQ | Échecs répétés de connexion/publication, backoff, reprise, même enveloppe et confirmation ; timeout, connexion bloquée, NACK et non-routable |
| 5. Notifications continues pendant panne | 1 Hz virtuel pendant huit heures, session GPS conservée ; 2 000 callbacks et lectures de santé pendant un appel réseau bloqué dans un autre thread |
| 6. ESP32 éteint huit heures | Backoff existant renforcé par les véritables cycles applicatifs de découverte, aucun redémarrage passerelle |
| 7. Reconnexions GPS avec Victron/Ruuvi | Au moins 30 cycles GPS, fixtures Victron déchiffrées et températures Ruuvi publiées, un seul démarrage scanner passerelle |
| 8. GPS pendant GATT Inkbird | Deux clients connectés au même instant dans le modèle D-Bus, lecture Inkbird réussie, GPS conservé, reprise du scanner passerelle |
| 9. Arrêt/redémarrage GPS | Trois SIGTERM simulés et créations de service, session passerelle inchangée, annonces toujours traitées |
| 10. Arrêt après erreurs | Tests existants SIGTERM/timeout/annulation, backend D-Bus réel simulé, worker interrompu pendant confirmation, `join` borné et sous-processus sortant malgré worker bloqué |

Fichiers nouveaux : `tests/test_bluez_dbus.py` côté passerelle ;
`tests/amqp_simulator.py`, `tests/test_amqp_deadlines.py` et
`tests/test_isolation.py` côté GPS. `tests/test_coexistence.py` et
`tests/test_publishing.py` ont été renforcés. Les formats RabbitMQ, topics MQTT,
décodeurs et dépendances épinglées sont conservés. `git diff --check` passe
dans les deux dépôts. Les anciens résultats de construction d'images restent
historiques : aucune nouvelle image ARM ni image de déploiement n'est certifiée
par cette passe de tests sur sources montées.

Reproduction Windows depuis la passerelle, avec l'environnement existant :

```powershell
.\.venv\Scripts\python.exe -m pytest -q
Push-Location ..\ch.bus.burry-gps\low-level-esp32
& '..\..\ch.bus.bluetooth-mqtt\.venv\Scripts\python.exe' -m pytest -q
Pop-Location
```

Sous Linux/Python 3.12, installer `requirements-test.txt` de la passerelle puis
exécuter `python -m pytest -q -p no:cacheprovider` depuis chacun des deux
répertoires. Conserver les dépôts côte à côte pour les tests de coexistence.

## 4. Risques résiduels et validation Raspberry recommandée

Les tests ne reproduisent pas les capacités scan/connexions simultanées du
contrôleur, les interférences Wi-Fi/BLE, l'alimentation, le firmware, la charge
D-Bus réelle, les versions BlueZ/kernel, ni les autorisations Docker de production.
Une horloge de huit heures simulées ne constitue pas une endurance radio.
Le pair AMQP n'émule pas toutes les erreurs d'un vrai RabbitMQ. Les tests de
coexistence font partager une boucle de test aux objets applicatifs et ne
mesurent pas l'ordonnancement de processus réels. Un broker sain mais très lent
peut dépasser l'échéance de 10 s, provoquer une reconnexion et perdre/dupliquer
des positions conformément à la politique acceptée.

Procédure à exécuter ultérieurement dans une fenêtre de maintenance, avec accès
local de secours et retour arrière préparé selon `BLE_RELIABILITY.md` :

1. Relever versions BlueZ/kernel/firmware, images et configuration des deux
   conteneurs, sans imprimer les secrets. Construire les images finales depuis
   les sources validées ; conserver les anciennes images et les montages.
   Prévoir le délai d'arrêt Docker documenté de 120 s, permettant le nettoyage
   BLE et l'attente du worker. Ne pas modifier les autres services du van.
2. Établir une référence avec la passerelle seule : timestamps des mesures
   Victron/Ruuvi, lectures Inkbird, santé scanner et aucune annonce manquante
   durable. Mesurer les intervalles normaux plutôt que déduire la réception
   d'un simple message « scanner started ».
3. Activer ensuite les deux services et relever annonces, notifications GPS,
   lectures Inkbird, `rabbitmq.pending`, `dropped` et confirmations. Vérifier
   des connexions GPS pendant les lectures Inkbird, sans reset du contrôleur.
4. Simuler l'indisponibilité RabbitMQ **pour le seul conteneur GPS**, au moyen
   d'une configuration de test ou d'un proxy dédié, jamais en arrêtant un broker
   partagé ou en modifiant globalement le réseau. Maintenir huit heures avec
   GPS actif : notifications fraîches, file ≤128, pertes visibles, passerelle
   à son rythme habituel, aucun redémarrage BLE corrélé à la panne. Rétablir
   l'accès et vérifier la reprise des confirmations.
5. Éteindre l'ESP32 huit heures puis le rallumer plusieurs fois : pas de sortie
   GPS pour simple absence, retour des notifications et continuité Victron/Ruuvi.
   Pendant ces transitions, provoquer des lectures Inkbird et comparer leurs
   taux d'échec/latences à la référence.
6. Arrêter puis relancer uniquement le conteneur GPS, vérifier que la passerelle
   reste active et reçoit toujours. Tester l'arrêt des deux services pendant
   une panne de broker et après erreurs BLE ; relever délais, codes de sortie
   et absence de processus ou sessions orphelins après fermeture D-Bus.
7. Observer 48–72 h puis plusieurs semaines. En cas d'incident, corréler les logs
   UTC avec `btmon` et les journaux BlueZ/noyau avant toute action. Ces captures
   peuvent contenir des données sensibles et doivent rester dans le diagnostic
   local. Revenir aux images précédentes si les mesures stagnent au-delà du
   comportement de référence ou si les échecs GATT augmentent durablement.

N'introduire coordination interprocessus ou service BLE central qu'après une
reproduction mesurée d'un défaut de chevauchement. Deux clients Bleak ne
constituent pas à eux seuls la preuve d'un conflit. Aucun reset automatique
BlueZ/contrôleur/Raspberry n'est préconisé.

## Sources techniques

Sources locales examinées : code des deux branches, Pika 1.3.2
(`blocking_connection.py`, `connection.py`, `select_connection.py`) et Bleak
3.0.2 (`bluezdbus/scanner.py`, `manager.py`, `client.py`). Références primaires :
[sessions et filtres BlueZ Adapter1](https://github.com/bluez/bluez/blob/master/doc/org.bluez.Adapter.rst),
[BleakClient et découverte implicite](https://bleak.readthedocs.io/en/stable/api/client.html),
[Pika BlockingConnection : RPC, timers et threads](https://pika.readthedocs.io/en/1.3.2/modules/adapters/blocking.html).

**GO pour un déploiement contrôlé sur `hiva-oa` : isolation logicielle validée ;
coexistence radio réelle à confirmer sur Raspberry. Aucun déploiement effectué.**
