#!/usr/bin/env bash
#
# update-index.sh - refabrique les index depuis le corpus, puis bascule.
#
# A lancer SUR LE SERVEUR, depuis la racine du depot. Ne telecharge rien et ne
# featurise rien : le corpus (`data/corpus/<variante>` et
# `data/corpus/<variante>_unranked`) arrive de la machine de build, qui est la
# seule a avoir le dump ppy et les .osu non classes.
#
# Trois choses que la procedure manuelle rate systematiquement :
#
#   1. les DEUX corpus. `--corpus data/corpus/nomod` seul produit un index sans
#      une seule map non classee -- et rien ne le signale, l'index est
#      parfaitement valide, juste amputé de son pool.
#   2. `--manifest` et `--loved`. Sans eux les colonnes `status` et `playcount`
#      n'existent pas : les maps graveyard ressortent etiquetees comme venant du
#      dump, donc `ranked`. C'est un mensonge silencieux dans les resultats.
#   3. la construction se fait dans `data/index_new`, pas par-dessus l'index qui
#      sert. Une variante qui echoue a mi-chemin ne doit jamais pouvoir laisser
#      l'API avec un index a moitie ecrit.
#
# Usage :
#   ./deploy/update-index.sh                  # toutes les variantes du .env
#   ./deploy/update-index.sh nomod DT         # seulement celles-la
#   DRY_RUN=1 ./deploy/update-index.sh        # dit ce qu'il ferait
#
# Deux variables d'environnement, posees par le rafraichissement hebdomadaire
# (scripts/refresh_ranked.py du projet de recherche) :
#
#   EXTRA_CORPUS_SUFFIX=_delta
#       ajoute `data/corpus/<variante>_delta` a la liste des corpus, quand il
#       existe. C'est la ou atterrissent les maps classees depuis le dump.
#   MANIFEST_OVERRIDE=/data/refresh/annotations.json
#       remplace le manifeste du pool par une version fusionnee, qui porte en
#       plus les statuts suivis (`qualified`, et les maps repassees ranked).
#
# Les deux sont facultatives : sans elles le script se comporte exactement
# comme avant, ce qui compte parce qu'il reste lancable a la main.
#
set -euo pipefail

cd "$(dirname "$0")/.."
RACINE="$PWD"
COMPOSE="docker compose -f deploy/docker-compose.yml"
DATA="${OSUMAPMATCHER_HOST_DATA:-$RACINE/data}"
DRY_RUN="${DRY_RUN:-0}"

# les variantes viennent du .env, comme pour l'API : servir une variante qu'on
# n'a pas rebatie est la meilleure facon de croire un deploiement termine
if [ -f deploy/.env ]; then
    # shellcheck disable=SC1091
    set -a; . deploy/.env; set +a
fi
if [ "$#" -gt 0 ]; then
    VARIANTES="$*"
else
    VARIANTES="$(echo "${OSUMAPMATCHER_API_VARIANTS:-nomod}" | tr ',' ' ')"
fi

echo "racine    : $RACINE"
echo "donnees   : $DATA"
echo "variantes : $VARIANTES"
echo

# --- verifications avant de toucher a quoi que ce soit ------------------------

EXTRA_CORPUS_SUFFIX="${EXTRA_CORPUS_SUFFIX:-}"
MANIFEST_OVERRIDE="${MANIFEST_OVERRIDE:-}"

# Le corpus supplementaire est optionnel et sa liste est calculee ici, une
# fois : la construire deux fois (verification puis appel) est exactement le
# genre d'ecart qui fait batir un index sans le dossier qu'on venait de
# verifier.
corpus_de() {
    local v="$1"
    printf '%s' "/data/corpus/$v /data/corpus/${v}_unranked"
    if [ -n "$EXTRA_CORPUS_SUFFIX" ] && [ -d "$DATA/corpus/${v}${EXTRA_CORPUS_SUFFIX}" ]; then
        printf ' %s' "/data/corpus/${v}${EXTRA_CORPUS_SUFFIX}"
    fi
}

manquant=0
for v in $VARIANTES; do
    for d in "$DATA/corpus/$v" "$DATA/corpus/${v}_unranked"; do
        if [ ! -d "$d" ]; then
            echo "corpus absent : $d"
            manquant=1
        fi
    done
    if [ -n "$EXTRA_CORPUS_SUFFIX" ]; then
        extra="$DATA/corpus/${v}${EXTRA_CORPUS_SUFFIX}"
        if [ -d "$extra" ]; then
            echo "corpus additionnel : $extra ($(ls "$extra"/*.parquet 2>/dev/null | wc -l) chunks)"
        else
            echo "corpus additionnel absent, ignore : $extra"
        fi
    fi
done
MANIFESTE="$DATA/unranked/manifest.json"
MANIFESTE_CONTENEUR="/data/unranked/manifest.json"
if [ -n "$MANIFEST_OVERRIDE" ]; then
    # le chemin est donne vu du conteneur ; on le ramene cote hote pour la
    # verification d'existence, sinon on validerait un fichier qu'on ne lit pas
    MANIFESTE="$DATA${MANIFEST_OVERRIDE#/data}"
    MANIFESTE_CONTENEUR="$MANIFEST_OVERRIDE"
    echo "manifeste remplace : $MANIFESTE"
fi

for f in "$MANIFESTE" "$DATA/loved.json"; do
    if [ ! -f "$f" ]; then
        echo "annotation absente : $f"
        echo "  sans elle, les maps non classees ressortiraient etiquetees 'dump'."
        manquant=1
    fi
done
[ "$manquant" -eq 0 ] || { echo; echo "rien n'a ete modifie."; exit 1; }

echo "manifeste : $(python3 -c "
import json,sys
p=json.load(open('$MANIFESTE'))
print(f\"{p.get('n_total', len(p.get('beatmaps', {})))} maps, du {p.get('fetched_at','?')}\")
print('           criteres :', p.get('criteria') or 'non enregistres')
")"
echo

if [ "$DRY_RUN" = "1" ]; then
    echo "DRY_RUN=1 : arret avant la construction."
    exit 0
fi

# --- construction, a cote de l'index qui sert ---------------------------------

rm -rf "$DATA/index_new"
mkdir -p "$DATA/index_new"

echo "# image du builder"
$COMPOSE --profile setup build builder

for v in $VARIANTES; do
    echo
    echo "# index $v"
    # shellcheck disable=SC2046,SC2086
    $COMPOSE --profile setup run --rm builder \
        python scripts/build_index.py \
            --corpus $(corpus_de "$v") \
            --manifest "$MANIFESTE_CONTENEUR" \
            --loved /data/loved.json \
            --out "/data/index_new/$v"
done

# Une variante incomplete passerait les etapes precedentes sans un mot : le
# builder peut sortir en 0 apres avoir ecrit une partie seulement.
#
# Les cinq noms viennent des constantes de src/osumapmatcher/index.py
# (SCALER_FILE, VECTORS_FILE, RAW_FILE, INDEX_FILE, DB_FILE). Les verifier
# toutes plutot qu'une seule : c'est exactement l'oubli qui a fait echouer la
# premiere repetition de ce script, sur un `maps.sqlite` qui n'a jamais existe.
ATTENDUS="scaler.npz vectors.npy vectors_raw.npy index.usearch corpus.sqlite"
for v in $VARIANTES; do
    for f in $ATTENDUS; do
        if [ ! -s "$DATA/index_new/$v/$f" ]; then
            echo
            echo "index_new/$v/$f est absent ou vide."
            echo "L'index en service n'a pas bouge."
            exit 1
        fi
    done
done

# --- bascule ------------------------------------------------------------------

echo
echo "# bascule"
$COMPOSE stop api
rm -rf "$DATA/index_prev"
[ -d "$DATA/index" ] && mv "$DATA/index" "$DATA/index_prev"
mv "$DATA/index_new" "$DATA/index"
$COMPOSE up -d api

# --- verification, et retour en arriere si ca ne repond pas -------------------

PORT="${OSUMAPMATCHER_BIND:-127.0.0.1:8080}"
PORT="${PORT##*:}"
echo
echo -n "attente de /health sur 127.0.0.1:$PORT "
pret=0
for _ in $(seq 1 60); do
    if curl -fsS --max-time 4 "http://127.0.0.1:$PORT/health" 2>/dev/null \
        | grep -q '"ready":true'; then
        pret=1
        break
    fi
    echo -n "."
    sleep 2
done
echo

if [ "$pret" -ne 1 ]; then
    echo "l'API ne repond pas prete. Retour a l'index precedent."
    $COMPOSE stop api
    rm -rf "$DATA/index_ko"
    mv "$DATA/index" "$DATA/index_ko"
    mv "$DATA/index_prev" "$DATA/index"
    $COMPOSE up -d api
    echo "index fautif garde dans $DATA/index_ko"
    exit 1
fi

curl -fsS "http://127.0.0.1:$PORT/health"
echo
echo
echo "en service. L'index precedent est dans $DATA/index_prev (a supprimer"
echo "quand tu es sur de toi, ou a remettre en place avec :"
echo "  $COMPOSE stop api && mv $DATA/index $DATA/index_ko \\"
echo "    && mv $DATA/index_prev $DATA/index && $COMPOSE up -d api)"
