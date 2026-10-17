# Surveillance Doctolib

Surveille uniquement les créneaux de **première consultation / nouveau patient** du Dr Maria Daniela Aguilar Pardo à Laval et alerte un salon Discord. Le programme ne sélectionne et ne réserve jamais de créneau.

## Hébergement gratuit

Ce dépôt doit être **public** pour que les runners GitHub Actions standards soient gratuits. Le webhook reste un secret GitHub Actions nommé `DISCORD_WEBHOOK_URL` et ne figure jamais dans le dépôt ou les logs.

Doctolib bloque actuellement l'API de ses créneaux depuis les adresses IP de
GitHub (HTTP 403). Le workflow cloud ne sert donc qu'à vérifier périodiquement
si ce blocage est levé ; la surveillance active s'exécute sur le Mac.

Le service local contrôle environ toutes les 20 à 25 minutes. Après une réponse
HTTP 429, il attend deux heures afin de ne pas prolonger la limitation. Il
conserve aussi l'historique de tous les créneaux déjà signalés : un créneau
maintenu, ou qui disparaît puis réapparaît, ne produit pas de seconde alerte.

GitHub peut désactiver un workflow planifié sur un dépôt public après 60 jours sans activité. Il faut alors le réactiver dans l'onglet Actions.

## Arrêt

Dans GitHub : **Actions → Surveillance Doctolib → … → Disable workflow**.
