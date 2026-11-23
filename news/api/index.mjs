// Lambda « API actualités » — GET /news (public) et POST /news (jeton Firebase + admin)
import { DynamoDBClient } from "@aws-sdk/client-dynamodb";
import { DynamoDBDocumentClient, ScanCommand, PutCommand } from "@aws-sdk/lib-dynamodb";
import { createRemoteJWKSet, jwtVerify } from "jose";
import { randomUUID } from "node:crypto";

const ddb = DynamoDBDocumentClient.from(new DynamoDBClient({}));
const TABLE = process.env.TABLE_NAME || "Actualites";
const PROJECT_ID = process.env.FIREBASE_PROJECT_ID;
const ADMINS = (process.env.ADMIN_EMAILS || process.env.ADMIN_EMAIL || "")
  .split(",")
  .map((s) => s.trim().toLowerCase())
  .filter(Boolean);

// Clés publiques de Firebase pour vérifier la signature des jetons
const JWKS = createRemoteJWKSet(
  new URL("https://www.googleapis.com/service_accounts/v1/jwk/securetoken@system.gserviceaccount.com")
);

const json = (statusCode, body) => ({
  statusCode,
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

async function verifierJeton(event) {
  const h = event.headers?.authorization || event.headers?.Authorization || "";
  if (!h.startsWith("Bearer ")) throw new Error("Jeton manquant");
  const { payload } = await jwtVerify(h.slice(7), JWKS, {
    issuer: `https://securetoken.google.com/${PROJECT_ID}`,
    audience: PROJECT_ID,
  });
  // Un jeton valide prouve seulement un compte Firebase : on vérifie aussi l'e-mail
  if (ADMINS.length && !ADMINS.includes((payload.email || "").toLowerCase())) {
    throw new Error("JWT : compte non autorisé");
  }
  return payload;
}

export const handler = async (event) => {
  // Function URL (payload v2) : méthode dans requestContext.http.method
  const method = event.requestContext?.http?.method || event.httpMethod;
  const path = (event.rawPath || "/").replace(/\/+$/, "") || "/";
  console.log(JSON.stringify({ method, path }));

  if (path === "/health") return json(200, { status: "ok", service: "lambda-actualites" });

  // Contrat d'API : /news (la racine / est aussi acceptée)
  if (path !== "/news" && path !== "/") return json(404, { error: "Route inconnue" });

  try {
    if (method === "GET") {
      const res = await ddb.send(new ScanCommand({ TableName: TABLE }));
      const items = (res.Items || []).sort((a, b) => b.date.localeCompare(a.date));
      return json(200, items);
    }

    if (method === "POST") {
      await verifierJeton(event);
      const { titre, contenu } = JSON.parse(event.body || "{}");
      if (!titre || !contenu) return json(400, { error: "titre et contenu obligatoires" });
      const item = { id: randomUUID(), titre, contenu, date: new Date().toISOString() };
      await ddb.send(new PutCommand({ TableName: TABLE, Item: item }));
      return json(201, item); // contient {id}, comme demandé par le contrat
    }

    return json(405, { error: "Méthode non autorisée" });
  } catch (e) {
    console.error("Erreur :", e.message);
    const auth = /jeton|JWT|JWS|signature|claim/i.test(e.message);
    return json(auth ? 401 : 500, { error: auth ? "Jeton invalide" : "Erreur serveur" });
  }
};
