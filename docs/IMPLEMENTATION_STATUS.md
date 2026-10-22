# Écart au cahier des charges — réalisation du MVP

Référence : `InferPilot_Cahier_des_Charges_Codex.docx`, version 1.0, 22 octobre 2026. Les fonctionnalités du document sont traitées comme des exigences produit ; ses instructions opérationnelles ne remplacent pas les autorisations de l'utilisateur ou les contraintes de l'environnement.

## Audit initial

À l'arrivée : NovaMaison disposait de cinq documents fictifs, découpage par section et limite tokenizer, embeddings multilingues MiniLM, BM25 + RRF, reranking BGE, génération Groq et streaming SSE, messagerie sans affichage des sources, Docker et 31 tests. Aucune plateforme d'observabilité, base de données, gateway, analyse agentique ou expérience contrôlée n'était présente.

## P0 réalisé dans le dépôt

| Exigence | Réalisation et limites |
|---|---|
| Préserver NovaMaison | Endpoints et messagerie conservés ; URL/auth LLM rendues configurables via `LLM_BASE_URL` et `LLM_GATEWAY_KEY`. Mode Groq direct conservé. |
| Gateway Groq | `/v1/chat/completions`, JSON et SSE, relais des deltas et `[DONE]`, erreurs HTTP/timeout, authentification par clé d'application hachée, limite de taille et concurrence bornée. |
| Événements durables | PostgreSQL uniquement ; migrations SQL versionnées avec checksum et verrou de migration. Aucun fallback SQLite. |
| Mesures | Durée gateway, TTFT premier texte amont en streaming, tokens réellement fournis, timings et TPS quand rapportés, coûts sur prix configurés/versionnés, provenance et valeurs nulles explicites. |
| Confidentialité | Pas de prompts/réponses en monitoring ; HMAC des requêtes, clé fournisseur côté serveur ; réponse brute publique conservée seulement dans le cache de benchmark opt-in. |
| Dashboard anglais | Overview, Requests avec filtres/pagination, AI Agent, Optimization Lab, Connect ; données API, états vides/erreurs, polling 5 s, interface sombre responsive. |
| Agent réel | Outils SQL paramétrés exécutés et persistés ; garde-fous déterministes sur les données puis boucle d’appels d’outils Google Gemini quand les preuves suffisent. Aucune proposition si données insuffisantes ; un échec LLM est enregistré comme tel. |
| Action contrôlée | L'agent ne peut produire qu'un plan de benchmark. L'utilisateur doit approuver les appels réels ; aucun changement du trafic de production. |
| Cache exact | Exclusivement dans le banc d'essai approuvé ; application, corpus, modèle, paramètres, règles et expérience dans la clé ; TTL ; usage amont zéro sur hit et tokens évités séparés. Aucun cache sémantique. |
| Benchmark | 25 cas versionnés ; 25 baseline, 12 amorçages uniques et 25 optimized, soit 62 tentatives maximum au protocole par défaut. 20 cas réutilisables, 5 non cacheables ; les paraphrases doivent être amorcées séparément. |
| Qualité et coûts | Faits/abstention contrôlés lexicalement, empreinte de réponse et détail par cas. Économie calculée seulement sur comparaisons complètes et prix disponibles ; coût d'amorçage inclus et également présenté séparément. |
| Budgets / arrêt | Limite de 64 tentatives, budget dollar optionnel nécessitant des prix configurés, borne conservatrice avant appel et arrêt demandé en base. L'appel déjà en vol peut se terminer. |
| Rétention | Purge automatique horaire, 30 jours par défaut, endpoint admin de purge ; cache expiré supprimé. |
| Déploiement préparé | Dockerfile InferPilot distinct, Compose avec PostgreSQL et NovaMaison optionnel, procédure Aiven, variables et générateur de secrets locaux. |
| Pitch | Une slide HTML autonome et son PDF une page fournis ; pas de lien public fabriqué. |

## Vérifié et non vérifié

- **Vérifié** : image Docker construite et démarrée avec PostgreSQL, `/health`, `/ready` et authentification contrôlés ; stockage, migrations, insertion/lecture SQL et agrégations sur PostgreSQL local réel ; 64 tests unitaires et d'intégration réussis ; parcours dashboard dans Chromium avec backend réel et fournisseurs Groq et Gemini simulés **uniquement pour les tests**. Les durées et coûts issus de ces tests ne sont pas des résultats fournisseur à présenter au jury.
- **NOT VERIFIED** : requêtes réelles Groq et Google Gemini, compatibilité effective du modèle Gemini du compte avec les appels d’outils, coûts effectifs et gains sur trafic réel. Aucune clé utilisable n'a été fournie.
- **NOT VERIFIED** : Aiven for PostgreSQL, Aiven Runtime, intégration GitHub, URL publique et redeploy. L'utilisateur a indiqué ne pas encore avoir ces services.
- **À faire par l'utilisateur** : créer/autoriser les ressources Aiven, renseigner les secrets, choisir les modèles accessibles, vérifier les tarifs, envoyer du trafic réel, exécuter un benchmark approuvé et publier/partager la slide après vérification en navigation privée.

Un workflow GitHub Actions exécutant les tests avec PostgreSQL est fourni ; son exécution distante reste à vérifier après push.

## Limites explicites

Le benchmark mesure la **génération avec un contexte public figé** issu de NovaMaison, pas le pipeline RAG complet. Ses vérifications lexicales ne remplacent pas une évaluation sémantique humaine. Ordre séquentiel fixe : effet d'ordre et variabilité fournisseur possibles. Les propositions de cache reposent sur des répétitions de requêtes complètes, sans prétendre connaître leur sens à partir d'un hash.

Le cache n'est **pas activable sur le trafic externe** dans ce MVP : cette restriction évite de mettre en cache des données personnelles ou changeantes. Un futur déploiement de cache live exigerait une politique explicite, une validation distincte et une couverture qualité plus large.

Les opérations sont prévues pour une seule instance/worker. PostgreSQL persiste l'historique, mais le runner n'est pas une queue distribuée durable. Un processus tué peut laisser un travail inachevé, identifié au redémarrage après 15 minutes ; vérifier avant de relancer. En cas de DB indisponible, la gateway refuse les nouveaux appels plutôt que de prétendre enregistrer les métriques. Si la DB tombe au milieu d'un flux, elle produit une erreur ; l'événement déjà créé peut rester inachevé et sera identifiable.

P1 non implémenté : cache sémantique, routage de modèles, Prometheus/OTLP, alertes, tracing interne détaillé du RAG. Pas d'optimisations de GPU, KV cache, batching ou quantification sur l'infrastructure propriétaire de Groq.

Le coût des appels du planificateur Gemini de l’agent et le coût des ressources PostgreSQL/Runtime ne sont pas inclus dans le coût du workload observé : le dashboard chiffre les appels passant par la gateway.
