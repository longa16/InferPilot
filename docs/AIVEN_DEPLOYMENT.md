# Déploiement Aiven — à effectuer par le propriétaire du compte

**Statut : NOT VERIFIED.** Aucun compte, service PostgreSQL Aiven ou accès Runtime n'a été fourni. Le PostgreSQL local utilisé pour les tests ne satisfait pas la contrainte de démonstration finale sur Aiven.

## Préparer les accès

1. Vérifier dans la console Aiven que le projet a accès à **Runtime**, les crédits nécessaires et le droit de créer un service PostgreSQL. La disponibilité et les quotas doivent être confirmés sur le compte du hackathon.
2. Connecter GitHub à Aiven et autoriser `longa16/InferPilot`. Cela doit se faire par le parcours d'autorisation du propriétaire ; ne pas créer de token GitHub à inclure dans le code.
3. Créer **Aiven for PostgreSQL**, attendre son état RUNNING, puis créer l'application Runtime depuis le dépôt, branche `main`, avec **`Dockerfile.inferpilot`**. NovaMaison est un composant séparé : ne pas sélectionner son Dockerfile pour la plateforme.
4. Connecter le service PostgreSQL à l'application par l'intégration `application_service_credential`, selon les capacités effectivement disponibles, et exposer `DATABASE_URL`.

## Configuration

Le conteneur démarre sur `0.0.0.0:$PORT`, 8080 par défaut, avec **un worker** pour ce MVP. Les migrations SQL versionnées sont appliquées transactionnellement au démarrage, sous verrou PostgreSQL. Le compte DB doit pouvoir créer les tables/index du schéma choisi. En cas d'échec, le démarrage échoue ; aucune base SQLite de repli n'est utilisée.

Définir dans les paramètres **secrets** Aiven :

- `DATABASE_URL` : URL PostgreSQL réelle, avec TLS explicite. Préférer `sslmode=verify-full` et configurer la CA Aiven via `sslrootcert`/`PGSSLROOTCERT` selon le mécanisme de montage disponible. Ne pas désactiver la validation TLS pour contourner une erreur. `sslmode=require` chiffre mais ne remplace pas une vérification complète de l'identité du serveur.
- `ADMIN_AUTH_SECRET` : aléatoire, au moins 32 caractères ; accès aux données du dashboard.
- `HMAC_SECRET` : autre secret aléatoire stable, au moins 32 caractères ; empreintes non réversibles des requêtes. Le remplacer invalide leur corrélation et les clés du cache.
- `GROQ_API_KEY` : clé du fournisseur, accessible uniquement au backend.

Définir les paramètres non secrets :

- `ENVIRONMENT=production`, `PORT` selon la configuration Runtime.
- `GROQ_MODEL` et `AGENT_MODEL` : modèles effectivement accessibles au compte. Le modèle de l'agent doit prendre en charge les appels d'outils.
- `RETENTION_DAYS=30` par défaut.
- `PRICING_JSON` : objet des prix courants **vérifiés par l'opérateur**, contenant pour chaque modèle `input_per_million`, `output_per_million`, `version` et `verified_at`. Sans prix, le dashboard affiche N/A. Aucun tarif fournisseur n'a été inventé ou intégré.

Autoriser l'egress HTTPS vers `api.groq.com` et la connexion PostgreSQL Aiven (hôte et port attribués, souvent différents de 5432). Le conteneur InferPilot ne télécharge pas les modèles d'embedding ; c'est NovaMaison qui a besoin de Hugging Face.

## Vérification à réaliser après déploiement

Utiliser uniquement **l'URL réelle fournie par Aiven**, pas une URL supposée :

1. GET `/health` : processus vivant. GET `/ready` : connexion PostgreSQL fonctionnelle ; cela ne prouve pas que la clé ou le modèle Groq fonctionne.
2. Ouvrir le dashboard et se connecter avec `ADMIN_AUTH_SECRET`. Créer une application NovaMaison ; copier sa clé gateway une seule fois dans le gestionnaire de secrets de NovaMaison.
3. Configurer NovaMaison : `LLM_BASE_URL=https://<hôte-réel>/v1`, `LLM_GATEWAY_KEY=<clé-gateway>` et le même modèle Groq. La clé Groq n'est pas nécessaire côté NovaMaison dans ce mode.
4. Envoyer des questions réelles. Vérifier successivement la réponse non-stream, le streaming progressif, `[DONE]`, l'augmentation des événements PostgreSQL et les provenances de tokens/coûts. Vérifier aussi le refus d'une mauvaise clé gateway.
5. Répéter quelques questions, lancer l'agent et inspecter ses **appels d'outils réels**. Approuver le benchmark ; attendre un rapport complet, examiner les coûts d'amorçage et la qualité avant de parler de gains.
6. Faire un redeploy explicite du dernier commit, puis refaire `/ready` et une requête. **Ne pas supposer qu'un push déclenche automatiquement un déploiement.**
7. Capturer les résultats réels pour le jury. Les tests avec Groq simulé ne sont pas une preuve de gain fournisseur ni de déploiement Aiven.

Le cache ne s'applique pas au trafic externe : seule l'expérience isolée approuvée peut l'activer. L'état durable est en PostgreSQL. Une interruption de processus peut laisser une expérience en cours : les travaux vieux de plus de 15 minutes sont marqués interrompus au démarrage suivant. Le MVP est prévu pour une seule instance ; ne pas prétendre à une orchestration distribuée durable.

## En cas d'absence d'accès Runtime

Le code et le conteneur restent prêts à être déployés ; conserver Aiven PostgreSQL comme cible de données et noter **Runtime NOT VERIFIED / unavailable on account**. Un hébergement local ou autre ne doit pas être présenté comme satisfaisant l'exigence Runtime.

Références à consulter et confronter aux capacités du compte :

- https://aiven.io/docs/products/runtime/deploy-apps
- https://aiven.io/docs/products/runtime/connect-services-to-apps
- https://aiven.io/docs/products/runtime/connect-github-account
