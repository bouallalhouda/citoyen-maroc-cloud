"""API KPI - CitoyenCloud Maroc (service PaaS conteneurise, heberge sur Render).

Role :
  - Ecriture : recoit chaque semaine le lot de KPI valides envoye par l'ETL (AWS EC2),
    protege par une cle d'API, et l'enregistre dans MongoDB Atlas.
  - Lecture  : sert les indicateurs au dashboard du site (React).

Configuration par variables d'environnement :
  MONGODB_URI      chaine de connexion MongoDB Atlas (secret)
  MONGODB_DB       nom de la base (defaut : citoyencloud_kpi)
  API_KEY          cle d'API d'ingestion partagee avec l'ETL (secret)
  ALLOWED_ORIGINS  origines CORS autorisees, separees par des virgules
                   (adresse du site Azure App Service ; defaut : site Azure + localhost)
"""

import hmac
import logging
import os
from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, model_validator
from pymongo import MongoClient
from pymongo.database import Database

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("kpi-api")


# --------------------------------------------------------------------------- #
# Format des donnees (contrat avec l'ETL de Houda) - PROVISOIRE
# Si le format change, modifier uniquement les trois classes ci-dessous.
# --------------------------------------------------------------------------- #
class Rank(BaseModel):
    rank: int = Field(ge=1)
    total: int = Field(ge=1)

    @model_validator(mode="after")
    def rank_within_total(self):
        if self.rank > self.total:
            raise ValueError("le rang ne peut pas depasser le nombre total de pays")
        return self


class Rankings(BaseModel):
    world: Optional[Rank] = None
    africa: Optional[Rank] = None
    mena: Optional[Rank] = None


Category = Literal["health", "education", "economy"]


class Indicator(BaseModel):
    code: str = Field(min_length=1, max_length=64)  # ex. SP.DYN.LE00.IN
    name: str = Field(min_length=1, max_length=200)
    category: Category
    unit: Optional[str] = Field(default=None, max_length=32)
    year: int = Field(ge=1960, le=2100)
    value: float
    rankings: Rankings = Field(default_factory=Rankings)


class IngestBatch(BaseModel):
    batch_id: str = Field(min_length=1, max_length=64)  # ex. 2026-W47
    generated_at: Optional[datetime] = None
    source: str = Field(default="World Bank", max_length=100)
    indicators: list[Indicator] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def unique_code_year(self):
        keys = [(i.code, i.year) for i in self.indicators]
        if len(keys) != len(set(keys)):
            raise ValueError("couples (code, annee) dupliques dans le lot")
        return self


class IngestResult(BaseModel):
    batch_id: str
    received: int
    inserted: int
    updated: int


# --------------------------------------------------------------------------- #
# Base de donnees et securite
# --------------------------------------------------------------------------- #
_client: Optional[MongoClient] = None


def get_db() -> Database:
    """Retourne la base MongoDB (connexion creee a la premiere utilisation)."""
    global _client
    uri = os.getenv("MONGODB_URI", "")
    if not uri:
        raise HTTPException(status_code=503, detail="Base de donnees non configuree")
    if _client is None:
        _client = MongoClient(uri, serverSelectionTimeoutMS=5000)
    return _client[os.getenv("MONGODB_DB", "citoyencloud_kpi")]


def require_api_key(x_api_key: Optional[str] = Header(default=None)) -> None:
    """Verifie l'en-tete X-API-Key (comparaison a temps constant)."""
    expected = os.getenv("API_KEY", "")
    if not expected:
        raise HTTPException(status_code=503, detail="Cle d'API non configuree")
    if not x_api_key or not hmac.compare_digest(x_api_key.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="Cle d'API invalide")


# --------------------------------------------------------------------------- #
# Application
# --------------------------------------------------------------------------- #
app = FastAPI(
    title="API KPI - CitoyenCloud Maroc",
    description="Indicateurs de sante, d'education et d'economie du Maroc et classements.",
    version="1.2.0",
)

# Site Azure de Najia + tests locaux (surchargeable par ALLOWED_ORIGINS)
DEFAULT_ORIGINS = (
    "https://citoyencloud-maroc-hfcrexacd5djfdaa.italynorth-01.azurewebsites.net,"
    "http://localhost:8080,http://localhost:3000"
)
origins = [
    o.strip().rstrip("/")
    for o in os.getenv("ALLOWED_ORIGINS", DEFAULT_ORIGINS).split(",")
    if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-API-Key"],
)


@app.get("/health")
def health():
    """Point de supervision. Toujours 200 ; l'etat de la base est indique dans le corps."""
    status = {
        "status": "ok",
        "service": "kpi-api",
        "time": datetime.now(timezone.utc).isoformat(),
    }
    if not os.getenv("MONGODB_URI"):
        status["db"] = "not_configured"
    else:
        try:
            get_db().command("ping")
            status["db"] = "ok"
        except Exception:  # noqa: BLE001
            log.exception("Ping MongoDB en echec")
            status["db"] = "unreachable"
            status["db_error"] = type(exc).__name__
    return status


@app.post(
    "/kpi/ingest",
    response_model=IngestResult,
    dependencies=[Depends(require_api_key)],
)
def ingest(batch: IngestBatch, db: Database = Depends(get_db)):
    """Ingestion d'un lot valide par l'ETL (une ligne par indicateur ET par annee).

    Idempotent : un couple (code, annee) deja present est remplace.
    """
    now = datetime.now(timezone.utc)
    inserted = 0
    for ind in batch.indicators:
        doc = ind.model_dump()
        doc.update(
            {"_id": f"{ind.code}:{ind.year}", "batch_id": batch.batch_id, "updated_at": now}
        )
        # Ancien format (cle = code seul, avant l'historique) : on le supprime
        db.indicators.delete_one({"_id": ind.code})
        res = db.indicators.replace_one({"_id": doc["_id"]}, doc, upsert=True)
        if res.upserted_id is not None:
            inserted += 1

    db.batches.replace_one(
        {"_id": batch.batch_id},
        {
            "_id": batch.batch_id,
            "source": batch.source,
            "generated_at": batch.generated_at,
            "received_at": now,
            "count": len(batch.indicators),
        },
        upsert=True,
    )
    received = len(batch.indicators)
    log.info("Lot %s : %d recus, %d nouveaux", batch.batch_id, received, inserted)
    return IngestResult(
        batch_id=batch.batch_id,
        received=received,
        inserted=inserted,
        updated=received - inserted,
    )


def _latest_per_code(docs: list[dict]) -> list[dict]:
    """Garde, pour chaque code, la ligne de l'annee la plus recente."""
    latest: dict[str, dict] = {}
    for d in docs:
        if d["code"] not in latest or d["year"] > latest[d["code"]]["year"]:
            latest[d["code"]] = d
    return sorted(latest.values(), key=lambda d: (d["category"], d["code"]))


@app.get("/kpi")
def list_kpi(
    category: Optional[Category] = Query(default=None),
    db: Database = Depends(get_db),
):
    """Derniere valeur de chaque indicateur (filtre optionnel par categorie).

    L'historique complet est disponible par indicateur : GET /kpi/{code}/history.
    """
    query = {"category": category} if category else {}
    items = _latest_per_code(list(db.indicators.find(query, {"_id": 0})))
    last = db.batches.find_one(sort=[("received_at", -1)])
    return {
        "count": len(items),
        "last_update": last["received_at"] if last else None,
        "items": items,
    }


@app.get("/kpi/last-update")
def last_update(db: Database = Depends(get_db)):
    """Informations sur le dernier lot recu."""
    last = db.batches.find_one(sort=[("received_at", -1)])
    if not last:
        raise HTTPException(status_code=404, detail="Aucun lot recu pour le moment")
    last["batch_id"] = last.pop("_id")
    return last


@app.get("/kpi/{code}/history")
def get_kpi_history(code: str, db: Database = Depends(get_db)):
    """Serie chronologique d'un indicateur (annees croissantes), pour les courbes."""
    docs = list(db.indicators.find({"code": code}, {"_id": 0}).sort("year", 1))
    if not docs:
        raise HTTPException(status_code=404, detail="Indicateur introuvable")
    last = docs[-1]
    return {
        "code": code,
        "name": last["name"],
        "category": last["category"],
        "unit": last.get("unit"),
        "count": len(docs),
        "series": [
            {"year": d["year"], "value": d["value"], "rankings": d.get("rankings")}
            for d in docs
        ],
    }


@app.get("/kpi/{code}")
def get_kpi(code: str, db: Database = Depends(get_db)):
    """Un indicateur par son code (annee la plus recente)."""
    docs = list(db.indicators.find({"code": code}, {"_id": 0}))
    if not docs:
        raise HTTPException(status_code=404, detail="Indicateur introuvable")
    return max(docs, key=lambda d: d["year"])
