# ITLF — CITA Luxembourg vers RSS

Convertisseur Python sans dépendances externes : DATEX II 3.6 → RSS 2.0 → IFTTT → Facebook Pages.

## Installation

1. Créer un dépôt GitHub **public** nommé `itlf-cita-rss`, branche principale `main`.
2. Envoyer **tous les fichiers et dossiers de cette archive**, y compris `.github/workflows/rss.yml` (dossier caché).
3. Dans **Settings → Actions → General → Workflow permissions**, sélectionner **Read and write permissions**. Si une règle d'organisation empêche ce choix, demander l'autorisation correspondante.
4. Dans **Actions → Actualiser RSS CITA → Run workflow**, lancer une première fois. Ce premier passage mémorise les incidents en cours sans les publier (évite un déluge initial).
5. Dans **Settings → Pages → Build and deployment**, sélectionner **Deploy from a branch**, branche `main`, dossier `/docs`, puis **Save**.
6. Après le premier déploiement, tester `https://VOTRE-PSEUDO.github.io/itlf-cita-rss/rss.xml` dans un navigateur. La page doit afficher du XML RSS.
7. Dans IFTTT : **If This → RSS Feed → New feed item** avec cette URL ; **Then That → Facebook Pages → Create a status message**. Utiliser `EntryTitle` et `EntryContent` dans le message, puis connecter la page ITLF. Faire un test avant d'activer la publication.

## Fonctionnement et limites

- Le flux officiel CITA est `https://cita.lu/info_trafic/datex/situationrecord36` (licence CC0). Il est rafraîchi chaque minute, mais ce programme l'interroge environ toutes les 5 minutes. GitHub Actions peut être retardé.
- Uniquement les autoroutes A1, A3, A6 et A13, lorsque leur nom est présent dans les champs routiers. Les enregistrements sans autoroute identifiable sont ignorés.
- Les événements à date de début future, terminés ou suspendus sont ignorés. Les cas complexes de périodes de validité ne sont pas interprétés : **contrôler les publications avant une diffusion entièrement automatique**.
- Les identifiants CITA déjà vus sont conservés dans `docs/state.json` pour éviter les doublons. Les mises à jour d'un même incident ne déclenchent pas une seconde publication ; la fin d'incident n'est pas annoncée.
- Les 40 dernières nouvelles alertes restent dans le RSS. Ne pas supprimer `docs/state.json` sans comprendre les conséquences.
- Le parseur est volontairement conservateur et n'a **pas été validé contre une réponse XML CITA en direct** dans cet environnement. Vérifier les journaux du workflow et les données générées avant de relier Facebook.
- GitHub Pages est public ; le RSS et l'historique des identifiants sont visibles de tous. Aucune clé secrète Facebook n'est enregistrée dans GitHub.
- L'action Facebook Pages et la cadence des applets dépendent des fonctionnalités IFTTT disponibles au moment de la configuration.

## Test local

```bash
python convert.py --input tests/sample.xml
```

Le script écrit `docs/rss.xml` et `docs/state.json`. Le test initial ne publie aucun événement. Ne pas commiter les fichiers de test générés si vous souhaitez démarrer avec les événements réels du CITA.

## Dépannage

- `403`/`timeout` à la source : consulter les logs Actions, vérifier l'accès réseau de GitHub au CITA.
- RSS vide : normal au premier lancement ; attendre un nouvel incident admissible. Vérifier aussi si les champs DATEX II réels correspondent au parseur.
- `Permission denied` au `git push` : vérifier les permissions Actions, les protections de branche et le nom `main`.
- Page 404 : attendre le déploiement Pages et vérifier les réglages du dossier `/docs`.
