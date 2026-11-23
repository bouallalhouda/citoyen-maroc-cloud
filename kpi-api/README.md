# API KPI - CitoyenCloud Maroc

Service PaaS conteneurise (FastAPI + Docker) heberge sur **Render**, avec **MongoDB Atlas** comme base.

- **Ecriture** : l'ETL (AWS EC2) envoie chaque semaine un lot de KPI valides (`POST /kpi/ingest`, cle d'API).
- **Lecture** : le site React affiche le dashboard (`GET /kpi`).

## Routes

| Methode | Route | Acces | Role |
|---|---|---|---|
| GET | `/health` | public | Supervision (toujours 200, etat de la base dans le corps) |
| POST | `/kpi/ingest` | en-tete `X-API-Key` | Ingestion d'un lot (idempotent) |
| GET | `/kpi` | public | Derniere valeur de chaque indicateur ; filtre `?category=health`, `education` ou `economy` |
| GET | `/kpi/last-update` | public | Dernier lot recu |
| GET | `/kpi/{code}` | public | Un indicateur par son code (annee la plus recente) |
| GET | `/kpi/{code}/history` | public | Serie chronologique (annees croissantes), pour les courbes |
| GET | `/docs` | public | Documentation interactive (Swagger) |

## Format du lot d'ingestion (valide avec Houda)

Voir `sample_batch.json` (valeurs fictives) et `tests/lot_reel_2026-W48.json` (lot reel de Houda, 10 indicateurs). Resume :

```json
{
  "batch_id": "2026-W47",
  "generated_at": "2026-11-20T03:00:00Z",
  "source": "World Bank",
  "indicators": [
    {
      "code": "SP.DYN.LE00.IN",
      "name": "Esperance de vie a la naissance",
      "category": "health",
      "unit": "annees",
      "year": 2023,
      "value": 75.0,
      "rankings": {
        "world":  { "rank": 100, "total": 190 },
        "africa": { "rank": 8,   "total": 54 },
        "mena":   { "rank": 10,  "total": 21 }
      }
    }
  ]
}
```

Historique : le lot contient un objet par indicateur ET par annee (ex. 10 ans x 10 indicateurs = 100 objets, meme format). Le stockage se fait par couple (`code`, `year`), donc le meme code peut apparaitre plusieurs fois avec des annees differentes.

Regles : `category` = `health`, `education` ou `economy` ; `rank` <= `total` ; couples (code, annee) uniques dans un lot ;
un couple (code, annee) deja present est remplace. Reponse : `{batch_id, received, inserted, updated}`.
Pour changer le format, modifier uniquement les classes `Rank`, `Rankings`, `Indicator`, `IngestBatch` dans `main.py`.

## 1. MongoDB Atlas (base gratuite M0)

1. Creer un compte Atlas et un cluster **M0** (gratuit).
2. *Database Access* : creer un utilisateur avec le droit **readWrite** sur la base `citoyencloud_kpi` uniquement.
3. *Network Access* : autoriser `0.0.0.0/0` (les adresses IP de Render du plan gratuit ne sont pas fixes). Compenser par un mot de passe long.
4. *Connect > Drivers* : copier la chaine `mongodb+srv://...` (a mettre dans `MONGODB_URI`).

## 2. Lancer en local

```bash
python -m venv .venv && source .venv/bin/activate      # Windows : .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env        # puis remplir les valeurs
export $(grep -v '^#' .env | xargs)                     # Windows : definir les variables a la main
uvicorn main:app --reload
```

Ouvrir http://localhost:8000/docs.

Tests automatiques (sans Atlas) :

```bash
pip install pytest mongomock httpx
pytest -v
```

Test manuel de l'ingestion puis de la lecture :

```bash
curl -X POST http://localhost:8000/kpi/ingest \
  -H "Content-Type: application/json" -H "X-API-Key: $API_KEY" \
  -d @sample_batch.json

curl http://localhost:8000/kpi
curl "http://localhost:8000/kpi?category=health"
curl http://localhost:8000/kpi/SP.DYN.LE00.IN
curl http://localhost:8000/kpi/SP.DYN.LE00.IN/history
```

## 3. Test du conteneur Docker en local

```bash
docker build -t kpi-api .
docker run --rm -p 8000:8000 --env-file .env kpi-api
```

## 4. Deploiement sur Render

1. Pousser ce dossier dans un depot GitHub (le fichier `.env` est ignore par `.gitignore`).
2. Render > **New > Web Service** > choisir le depot.
3. *Runtime* : **Docker** (Render utilise le `Dockerfile`). *Instance type* : **Free**.
4. *Environment* : ajouter `MONGODB_URI`, `MONGODB_DB`, `API_KEY`, `ALLOWED_ORIGINS` (adresse du site Azure, sans `/` final, plus `http://localhost:8080` pour les tests ; a mettre a jour si la variable existe deja).
5. *Health Check Path* : `/health`.
6. Deployer. L'URL est du type `https://<nom>.onrender.com`.
7. Verifier : `https://<nom>.onrender.com/health` puis `/docs`.

Limite du plan gratuit : mise en veille apres 15 minutes sans trafic, reveil d'environ une minute.
Avant la demo, ouvrir `/health` une minute a l'avance. Demander a Houda de prevoir 2 ou 3 tentatives (retry) dans l'ETL.

## 5. Checklist de demo

1. Console Render : service, historique des deploiements, variables secretes (valeurs masquees), journaux.
2. Appel `GET /kpi` (ou `/docs`).
3. Donnees dans MongoDB Atlas (Browse Collections > `indicators`, `batches`).
4. Dashboard du site mis a jour apres un envoi de l'ETL.

## 6. A communiquer a l'equipe

- **Houda** : URL de l'API, route `POST /kpi/ingest`, format du lot, cle d'API (transmission privee, jamais dans le depot), retry.
- **Najia** : URL de l'API, routes `GET`, exemple de reponse, adresse du site pour `ALLOWED_ORIGINS`.
