# InferPilot · NovaMaison

Assistant RAG pour une **entreprise e-commerce fictive**, interface web française et API FastAPI. Le corpus comprend quatre produits, les informations de l'entreprise, les livraisons, paiements, retours, garanties et FAQ. Toutes les données commerciales sont fictives.

## Pipeline

1. Lecture des fichiers Markdown dans `documents/` ; découpage par titre puis fenêtres de **180 mots**, chevauchement de **30 mots**, conservation du titre et du fichier source. Les fenêtres sont raccourcies avec le tokenizer du modèle pour respecter ses **128 tokens**, titre et tokens spéciaux inclus.
2. Embeddings multilingues locaux ONNX : `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` (384 dimensions). Vecteurs normalisés, similarité cosinus en mémoire.
3. Recherche lexicale BM25 + recherche vectorielle, 12 candidats par branche. Fusion **RRF** avec constante 60 ; exclusion des résultats lexicaux sans correspondance.
4. Reranking des 12 candidats fusionnés avec le cross-encoder multilingue **`BAAI/bge-reranker-v2-m3-int8`** ; sélection des 4 premiers.
5. Génération via **l’API Groq**, modèle par défaut `llama-3.3-70b-versatile`, température 0, réponse limitée à 800 tokens. Prompt limité aux sources et citations `[S…]`. Retour des passages et scores à côté de la réponse.

Pas de base vectorielle ni de GPU requis : adapté à un petit catalogue. L'index est reconstruit au démarrage ; le cache des modèles est conservé. Après modification des documents, redémarrer l'application. Chaque question est indépendante, sans mémoire conversationnelle.

## Déploiement Docker (avec génération)

Prérequis : Docker avec Compose, accès Internet aux registres et modèles, clé API Groq, environ **4 Go de RAM disponibles** pour la recherche et le reranking locaux. Le LLM est hébergé par Groq. Les premiers téléchargements de modèles locaux peuvent prendre plusieurs minutes.

```sh
cp .env.example .env
# Renseigner GROQ_API_KEY dans .env avec votre éditeur (ne pas committer ce fichier).
docker compose up --build -d
docker compose logs -f app
```

Compose lance uniquement l'application et conserve le cache des modèles locaux. La page est disponible sur le port local 8000 ; `/docs` expose la documentation API. Le port est lié à l'interface de boucle locale ; pour un hébergement public, placer un reverse proxy HTTPS devant l'application, avec authentification et limitation de débit.

```sh
curl -f http://localhost:8000/health
curl -f http://localhost:8000/api/chat -H 'Content-Type: application/json' \
  -d '{"question":"La gourde Terra passe-t-elle au lave-vaisselle ?"}'
docker compose down
```

`/health` valide le chargement de l'index, pas l'authentification ou les quotas Groq. La requête `/api/chat` valide le pipeline complet. Le volume `models` conserve les poids ; `down` ne le supprime pas. Pour choisir un autre modèle Groq disponible sur votre compte, modifier `GROQ_MODEL` dans `.env` puis relancer Compose.

## Développement local

Python 3.11+ et [uv](https://docs.astral.sh/uv/).

```sh
uv sync --frozen
cp .env.example .env
# Renseigner GROQ_API_KEY dans .env avant de lancer :
uv run --frozen uvicorn app.main:app --env-file .env --host 0.0.0.0 --port 8000
```

Variables : `GENERATION_MODE=groq` (défaut), `GROQ_API_KEY` (requise pour générer), `GROQ_MODEL=llama-3.3-70b-versatile` (modifiable). Créer une clé dans la [console Groq](https://console.groq.com/keys). Aucune clé n'est fournie ; accès et quotas dépendent du compte Groq. L'API reçoit la question et les quatre passages sélectionnés ; embeddings et reranking restent locaux. La clé est utilisée uniquement côté serveur et n'est jamais envoyée au navigateur.

Le fichier `.env` est ignoré par Git et exclu du contexte Docker. En cloud, privilégier le gestionnaire de secrets pour `GROQ_API_KEY`. Les valeurs peuvent aussi être injectées dans le processus sans fichier. Ne jamais partager la clé dans le chat.

Sans clé, le mode extractif permet de consulter les passages retrouvés, sans génération :

```sh
GENERATION_MODE=extractive uv run --frozen uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## Validation

```sh
uv run --frozen pytest -q
# Test d'intégration avec les vrais modèles (accès aux téléchargements nécessaire) :
GENERATION_MODE=extractive uv run --frozen python -m scripts.smoke
# Avec GROQ_API_KEY injectée, vérifie également les réponses et citations :
GENERATION_MODE=groq uv run --frozen python -m scripts.smoke
```

Les tests unitaires couvrent le découpage, BM25, RRF, l'utilisation du reranker, les validations API, le mode extractif et le contrat Groq avec doubles de test (authentification, choix du modèle, citations, réponses vides ou tronquées et erreurs HTTP 401/429/500). Ils ne remplacent pas les tests avec les vrais modèles. `scripts/smoke.py` vérifie la recherche en français, anglais et allemand.

Les téléchargements nécessitent `huggingface.co`, ses sous-domaines et `*.hf.co`. La génération nécessite `api.groq.com` et Docker nécessite les registres Docker ainsi que leurs destinations de stockage. En cas de refus du proxy, autoriser les destinations nécessaires sans désactiver TLS. La page ne sera servie qu'une fois les modèles chargés.

L'avertissement « You are sending unauthenticated requests to the HF Hub » n'est pas une erreur : les modèles utilisés sont publics et le téléchargement peut continuer sans authentification. Pour augmenter les limites de requêtes, créer un token **Read** dans les [paramètres Hugging Face](https://huggingface.co/settings/tokens), puis renseigner `HF_TOKEN` dans votre fichier `.env`. Compose transmet cette variable au conteneur ; en développement local, `uvicorn --env-file .env` la charge également. Ne partagez jamais ce token et ne le commitez pas. Après modification de `.env`, exécuter `docker compose up -d --force-recreate app`, puis `docker compose logs -f app`. Les poids déjà téléchargés restent dans le volume `models`. Un affichage à 0 % pendant le téléchargement d'un gros fichier ne suffit pas à établir un échec ; une erreur HTTP 429 ou une exception permet de diagnostiquer un blocage.

## Limites et évolution

Le mode extractif affiche les passages retrouvés, **sans produire de réponse synthétisée ni garantir leur pertinence**. En mode LLM, les citations inconnues sont rejetées, mais une citation valide ne prouve pas qu'une affirmation est correcte. Le prompt demande de reconnaître l'absence d'information ; ce n'est pas une garantie contre les hallucinations ou injections. Les questions hors catalogue, les demandes de stock et les opérations sur commandes doivent être évaluées avant un usage réel.

Pas de connecteur commandes, paiement, stock temps réel, historique, authentification ni rate limiting. N'envoyer aucune donnée personnelle. Les logs applicatifs n'enregistrent pas volontairement les questions, mais les exceptions peuvent contenir des détails techniques : contrôler leur accès. En production, ajouter évaluation métier, supervision, politique de conservation, contrôle d'accès et limites de requêtes. La recherche en mémoire et la sérialisation des requêtes conviennent à une démo à faible trafic.

## État vérifié dans l'environnement cloud

- Installation locale avec `uv sync --frozen` : réussie.
- Quatorze tests unitaires/API : réussis (modèles et API Groq simulés dans ces tests).
- Validation du fichier Compose : réussie ; un seul service applicatif.
- Chargement des vrais embeddings : bloqué par HTTP 403 du proxy sur Hugging Face.
- Construction Docker : bloquée par la résolution réseau pendant le téléchargement des dépendances dans le conteneur de build.
- Appel réel à Groq : **non validé**, clé absente et accès à `api.groq.com` refusé par le proxy.
- Recherche avec les vrais modèles et démarrage complet : **non validés** à ce stade.

Les accès et instructions nécessaires sont enregistrés dans le brouillon de configuration cloud. Ils doivent être appliqués avant de relancer l'intégration ; l'enregistrement du brouillon ne les active pas dans la machine courante.
