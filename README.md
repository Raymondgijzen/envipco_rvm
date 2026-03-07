# Envipco RVM

Nieuwe schone Home Assistant integratie voor Envipco RVM machines.

## Waarom deze nieuwe integratie

Deze versie gebruikt expres een **nieuw domein**:

- oud: `envipco_eportal`
- nieuw: `envipco_rvm`

Daardoor zit oude entity history en oude registry-data van de vorige integratie niet meer in de weg.

## Wat anders is

- alleen **actieve bins** worden aangemaakt
- configuratie-nummers voor **bin limieten** verschijnen alleen voor actieve bins
- **CAN** en **PET** vergoeding zijn per automaat in te stellen als configuratie-entiteiten
- bin limieten staan als **configuratie-entiteiten per machine** op de apparaatpagina
- nette `async_unload_entry`
- HACS structuur aanwezig

## Installatie

Plaats deze repository in GitHub of kopieer de map:

`custom_components/envipco_rvm`

naar Home Assistant.

Herstart daarna Home Assistant en voeg de integratie **Envipco RVM** toe.

## Belangrijk

Deze integratie gebruikt nieuwe entity-id's en een nieuw domein. Daardoor moet je bestaande dashboards en automations aanpassen.
