# Validation des corrections BLE — 19 octobre 2026

Travail local uniquement, sur `codex/ble-reliability` dans les deux dépôts.
Aucun accès à `hiva-oa`, aucun déploiement/push, aucun changement firmware/OS,
aucun vrai secret utilisé dans les tests ou ajouté aux fichiers.

## Suites exécutées

| Environnement | Passerelle | GPS ESP32 | Total |
| --- | --- | --- | --- |
| Windows, Python 3.11.2, environnement virtuel local | 61 réussis | 32 réussis | 93 |
| Docker Linux amd64, Python 3.12, dépendances des images | 61 réussis | 32 réussis | 93 |

Avant modification : les 45 tests de la passerelle passaient. Le collecteur GPS
ne disposait pas de suite de tests. Les 48 nouveaux cas couvrent les garde-fous
et la compatibilité. Aucun test n'a été sauté dans les exécutions communes,
les deux dépôts étant présents côte à côte.

Commandes Windows utilisées depuis la racine de la passerelle :

```powershell
.\.venv\Scripts\python.exe -m pytest -q
Set-Location ..\ch.bus.burry-gps\low-level-esp32
& '..\..\ch.bus.bluetooth-mqtt\.venv\Scripts\python.exe' -m pytest -q
```

Les conteneurs de tests Linux ont monté les sources en lecture seule, installé
`requirements-test.txt`, puis exécuté
`PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider`.

## Couverture des scénarios demandés

| Scénario | Preuve automatisée |
| --- | --- |
| Scanner sans résultats | Huit heures simulées de silence, reprises de plus en plus espacées sans sortie de processus ; annonce confirmant ensuite le retour |
| Arrêt scanner refusé/bloqué | Erreur et timeout de stop ; état `cleanup_failed`, aucun scanner de remplacement |
| Échec StartDiscovery | Fakes `InProgress` et adaptateur absent, nettoyage avant remplacement et nombre de tentatives borné |
| Timeout D-Bus | Démarrage sans réponse, connexion, abonnement, arrêt et lecture bloqués |
| GATT Inkbird bloqué | Délais et déconnexion dans tous les chemins ; les autres sondes continuent |
| GPS interrompu | Abonnement puis perte de connexion, déconnexion explicite, conservation du message GPS |
| ESP32 absent plusieurs heures | Horloge simulée huit heures, délais 60/120/240/480/900 s, aucun compteur de panne ni sortie pour absence |
| Deux applications | Deux sessions de découverte simulées ; la libération/reprise de l'une conserve la session de l'autre |
| Reprise après erreurs | Succès d'annonce/notification remettant compteurs et délais à zéro |
| Récupération impossible | Limite d'échecs et cooldown ; aucune nouvelle acquisition après nettoyage incertain |
| Tâche ignorant l'annulation | Test injecté vérifiant le délai avant `_exit` et test dans un véritable sous-processus vérifiant code 1 sans blocage d'`asyncio.run` |
| Arrêt propre | SIGTERM simulé, annulation pendant connexion/découverte, absence de tâches résiduelles ; essais SIGTERM sur les deux vrais points d'entrée Docker |
| MQTT/RabbitMQ | Fixtures de messages existantes, unités GPS, files bornées, reconnexion, acquittements, non-fuite de secrets et callback d'acquittement sans inversion de verrous |

La simulation interapplications vérifie les cycles applicatifs sur le contrat
BlueZ. Elle ne démontre ni l'absence ni l'existence de contention radio réelle.
Les brokers sont simulés dans les suites ; aucun test d'intégration avec un vrai
broker MQTT/RabbitMQ n'a été effectué. Les essais des points d'entrée Docker
vérifient uniquement les brokers indisponibles et les sorties du processus.

## Images construites et exécutées

Docker Desktop Linux 29.8.2. Constructions réussies :

| Image locale | Architecture | Résultat |
| --- | --- | --- |
| `ch.bus.bluetooth-mqtt:ble-reliability` | amd64 | build et exécution réussis |
| `ch.bus.burry-gps/low-level-esp32:ble-reliability` | amd64 | build et exécution réussis |
| `ch.bus.bluetooth-mqtt:ble-reliability-arm64` | arm64 sous émulation | build, imports natifs et trois fixtures Victron réussis |
| `ch.bus.burry-gps/low-level-esp32:ble-reliability-arm64` | arm64 sous émulation | build et import d'app/Pika/DBus réussis |

Les images ARM64 contiennent Bleak 3.0.2 et dbus-fast 5.2.0. La passerelle contient
Paho 2.1.0, victron-ble 0.9.3 et pycryptodome 4.0.1 ; le GPS Pika 1.3.2.
Les vérifications confirment l'absence de `gcc` et de `bluetoothd` dans les deux
images finales ARM64. Les déchiffrements utilisent uniquement la clé synthétique
publique des fixtures. Aucun paquet système n'est installé sur le Pi.

Deux conteneurs locaux avec `--network none`, sans montage D-Bus, ont exercé les
points d'entrée réels : erreurs BLE comptées, attente de 60 s avant sortie
annoncée, SIGTERM pendant cette attente, puis code de sortie **0** pour chacun.
Ils ont été supprimés après contrôle. Deux autres exécutions jetables avec
`BLE_FAILURE_LIMIT=1` et `BLE_PROCESS_RESTART_DELAY_SECONDS=1` ont vérifié la
sortie naturelle **1** après temporisation pour chaque application. Le GPS
utilisait uniquement des identifiants fictifs et un broker local inexistant.

`git diff --check` et les vérifications d'espaces sur l'index passent dans les
deux dépôts. Les avertissements Git LF/CRLF reflètent la configuration Windows,
sans erreur de whitespace. Les avertissements pip concernent son exécution
dans les conteneurs jetables et une version de pip plus récente disponible.

## Limites explicites

La radio, les versions BlueZ/kernel de production, les autorisations D-Bus du
GPS, les capteurs réels, les brokers réels et l'endurance de plusieurs semaines
restent à valider. Une construction ARM64 émulée n'est pas un essai matériel.
Les nouvelles dépendances transitives et la base Docker ne sont pas figées par
digest. Le plan de validation, la collecte des preuves à la prochaine panne,
le retour arrière et l'annexe sur le reboot à chaud sont dans
[BLE_RELIABILITY.md](BLE_RELIABILITY.md).
