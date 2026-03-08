# Envipco RVM

Home Assistant integratie voor Envipco statiegeldautomaten via de ePortal API.

Deze repository is opgezet als een **schone opvolger** van de oudere `envipco_eportal` integratie.
Het nieuwe Home Assistant domein is bewust `envipco_rvm`, zodat oude history, oude entity-id's en registry-rommel van de vorige integratie niet door deze versie heen fietsen.

## Wat deze integratie doet

De integratie haalt gegevens op uit de Envipco ePortal API en maakt per machine duidelijke Home Assistant devices en entiteiten aan.

Belangrijkste functies:

- apparaatnaam op basis van **RVM-ID + type**
  - voorbeeld: `090290-Quantum`
- duidelijke entity-id's
  - voorbeeld: `sensor.090290_status`
- live status uit `rvmStats`
- reject-tellingen uit `rejects`
- statische machine/site metadata uit `siteData`
- bin-vulling, bin-limieten en bin-configuratie per machine
- tariefinstellingen per machine voor blik en PET
- opbrengst-sensoren per machine
- throttling-diagnose voor de Envipco API

## Belangrijke ontwerpkeuzes

### 1. `siteData` wordt niet constant gepolld
`siteData` is relatief statisch. Daarom wordt deze call **niet** bij elke update gebruikt.

`siteData` wordt alleen gebruikt bij:

- eerste setup
- scan for new machines
- expliciete metadata-refresh tijdens setup

Dat scheelt requests en voorkomt onnodige rate limiting.

### 2. `rvmStats` en `rejects` hebben aparte intervallen
De integratie ondersteunt twee losse polling-intervallen:

- **RVM stats interval**
- **Rejects interval**

Dat is expres zo gedaan, omdat statusinformatie vaak sneller moet verversen dan reject-statistieken.

### 3. Accepted totalen komen alleen uit `rvmStats`
Dagtotalen zoals blik/PET/glas worden bewust alleen gebaseerd op:

- `cans_accepted`
- `pet_accepted`
- `glass_accepted`

Dus **niet** op `BinInfoCountBinX`.

Waarom niet?
Omdat `BinInfoCountBinX` de **inhoud van de bak** is, niet het dagtotaal van geaccepteerde verpakkingen.

### 4. Actieve bins alleen wanneer logisch
De integratie maakt alleen bins aan die daadwerkelijk actief lijken op basis van materiaal, count of full-status.
Zo blijft de entity-lijst netter en beheerbaarder.

## Entiteiten

### Algemene sensors per machine

Voorbeeld voor machine `090290`:

- `sensor.090290_status`
- `sensor.090290_last_report`
- `sensor.090290_last_report_text`
- `sensor.090290_accepted_total`
- `sensor.090290_accepted_cans`
- `sensor.090290_accepted_pet`
- `sensor.090290_reject_total`
- `sensor.090290_reject_rate`
- `sensor.090290_revenue_today`
- `sensor.090290_revenue_can_today`
- `sensor.090290_revenue_pet_today`
- `sensor.090290_last_successful_update`
- `sensor.090290_api_throttle_status`
- `sensor.090290_api_throttle_remaining`

### Bin sensors

Per actieve bin kunnen onder andere deze sensors ontstaan:

- `sensor.090290_bin_1_count`
- `sensor.090290_bin_1_active_limit`
- `sensor.090290_bin_1_percentage`

### Configuratie-entiteiten

Per machine worden number-entiteiten aangemaakt voor lokale configuratie in Home Assistant:

- blik tarief
- PET tarief
- bin-limieten per actieve bin

Deze waarden worden **niet** teruggeschreven naar ePortal.
Ze worden alleen lokaal gebruikt voor berekeningen en dashboarding.

## API throttling / rate limiting

De Envipco API kan `HTTP 429` teruggeven als er te veel requests in korte tijd worden gedaan.

Deze integratie probeert daar netjes mee om te gaan:

- `rvmStats` throttling wordt herkend
- `rejects` throttling wordt herkend
- cached reject-data blijft behouden als rejects tijdelijk geremd worden
- diagnose-sensoren laten zien of de API momenteel geremd wordt en hoeveel seconden nog resten

Aanbevolen startwaarden:

- **RVM stats interval:** `300` seconden
- **Rejects interval:** `900` seconden

## Installatie via HACS

1. Voeg deze repository toe als custom repository in HACS of publiceer hem normaal op GitHub.
2. Controleer dat de repository-structuur exact zo is:

```text
repo-root/
├── hacs.json
├── README.md
└── custom_components/
    └── envipco_rvm/
        ├── __init__.py
        ├── api.py
        ├── config_flow.py
        ├── const.py
        ├── coordinator.py
        ├── manifest.json
        ├── number.py
        ├── sensor.py
        └── strings.json
```

3. Installeer de integratie via HACS.
4. Herstart Home Assistant.
5. Voeg **Envipco RVM** toe via Apparaten & Diensten.

## Installatie handmatig

Kopieer de map `custom_components/envipco_rvm` naar je Home Assistant installatie:

```text
/config/custom_components/envipco_rvm
```

Herstart daarna Home Assistant en voeg de integratie toe.

## Configuratie

Tijdens setup worden in de basis opgeslagen:

- gebruikersnaam
- wachtwoord
- RVM stats interval
- Rejects interval
- gevonden machines
- machine-metadata
- lokale tarieven
- lokale bin-limieten

Via **Opties** kun je later onder andere aanpassen:

- RVM stats interval
- Rejects interval
- scan for new machines
- blik tarief per machine
- PET tarief per machine

## Inline documentatie in de code

De belangrijkste Python-bestanden hebben bewust extra uitleg gekregen:

- `api.py`
  - alleen API-communicatie en parsing
- `coordinator.py`
  - polling, caching, throttling en afgeleide totalen
- `sensor.py`
  - uitlezentiteiten, dun gehouden
- `number.py`
  - lokale configuratie-entiteiten
- `config_flow.py`
  - setup en opties
- `__init__.py`
  - HA entry setup en registry-naming

## Ontwikkelnotities

### Waarom een nieuw domain?
Home Assistant bewaart entity-registry en device-registry data hardnekkig.
Door over te stappen van `envipco_eportal` naar `envipco_rvm` is de kans veel kleiner dat oude entity-id's, friendly names en historische registry-data deze integratie vervuilen.

### Waarom technische namen?
Store names en accountnamen zijn vaak instabiel of minder bruikbaar in automations.
Daarom is gekozen voor technische, stabiele namen zoals:

- apparaat: `090290-Quantum`
- entity: `sensor.090290_status`

Dat maakt dashboards, scripts en troubleshooting een stuk duidelijker.

## Bekende beperkingen

- Home Assistant kan oude entity-id's of friendly names vasthouden als de gebruiker die eerder handmatig wijzigde.
- `machineType` hangt af van beschikbare metadata uit `siteData`.
- als de API bepaalde accepted-velden niet terugstuurt, worden totalen `0` in plaats van dat er naar bin-counts wordt teruggevallen. Dat is expres zo gedaan om dagtotalen zuiver te houden.

## Versie

Deze repository is bedoeld voor release **v1.0.17**.
De tag op GitHub moet dus ook `v1.0.17` zijn.
