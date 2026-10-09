# Surveillance Doctolib cloud

Surveille uniquement les créneaux de **première consultation / nouveau patient** du Dr Maria Daniela Aguilar Pardo à Laval et alerte un salon Discord. Le programme ne sélectionne et ne réserve jamais de créneau.

## Hébergement gratuit

Ce dépôt doit être **public** pour que les runners GitHub Actions standards soient gratuits. Le webhook reste un secret GitHub Actions nommé `DISCORD_WEBHOOK_URL` et ne figure jamais dans le dépôt ou les logs.

Le workflow s'exécute environ toutes les cinq minutes, avec un jitter de 0 à 60 secondes. GitHub peut retarder une exécution planifiée. Après une réponse HTTP 429 de Doctolib, les contrôles sont différés d'au moins quinze minutes.

GitHub peut désactiver un workflow planifié sur un dépôt public après 60 jours sans activité. Il faut alors le réactiver dans l'onglet Actions.

## Arrêt

Dans GitHub : **Actions → Surveillance Doctolib → … → Disable workflow**.
