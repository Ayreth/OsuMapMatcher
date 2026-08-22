# -*- coding: utf-8 -*-
"""
Tests du mode distant : le client de l'API (remote.py) et la bascule du
serveur local entre les deux moteurs (server.py).

Rien ici ne touche le reseau. Ce qui est verifie n'est pas qu'un appel HTTP
fonctionne -- test_api.py s'en charge de bout en bout -- mais les decisions :
dans quel ordre les trois formes de question sont essayees, ce qui declenche un
repli, et le fait que le repli soit annonce plutot que silencieux.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from osureco import server as server_module
from osureco.features import FEATURE_VERSION
from osureco.remote import USER_AGENT, RemoteBackend, RemoteError


# --- client -------------------------------------------------------------------

class FakeRemote(RemoteBackend):
    """Un RemoteBackend dont seul le transport est remplace."""

    def __init__(self, answers, *, feature_version=FEATURE_VERSION, **kwargs):
        super().__init__("https://exemple.test", **kwargs)
        self.answers = answers          # chemin -> reponse, ou exception a lever
        self.calls: list[tuple[str, dict, bool]] = []
        self._feature_version = feature_version

    def _call(self, path, *, params=None, body=None, content_type="", timeout=None):
        self.calls.append((path, dict(params or {}), body is not None))
        if path == "/api/v1/meta":
            return {"feature_version": self._feature_version,
                    "variants": ["nomod", "DT"], "n_maps": {"nomod": 10, "DT": 10}}
        answer = self.answers.get(path)
        if isinstance(answer, Exception):
            raise answer
        if answer is None:
            raise RemoteError("not found", status=404)
        return answer


def _reply(source: str) -> dict:
    return {"query": {}, "matches": [], "count": 0, "variant": "nomod", "source": source}


def test_le_checksum_suffit_dans_le_cas_normal():
    remote = FakeRemote({"/api/v1/similar": _reply("corpus")})
    payload = remote.search(checksum="abc", features={"stars": 5.0}, raw=b"x")
    assert payload["source"] == "corpus"
    assert [call[0] for call in remote.calls] == ["/api/v1/similar"]
    assert remote.calls[0][1]["checksum"] == "abc"


def test_une_map_absente_du_corpus_bascule_sur_les_features():
    """404 sur le checksum n'est pas une panne : c'est une map non soumise."""
    calls = {"n": 0}

    class Remote(FakeRemote):
        def _call(self, path, *, params=None, body=None, content_type="", timeout=None):
            if path == "/api/v1/similar" and body is None:
                raise RemoteError("absent", status=404)
            if path == "/api/v1/similar" and body is not None:
                calls["n"] += 1
                calls["body"] = json.loads(body)
                return _reply("features")
            return super()._call(path, params=params, body=body,
                                 content_type=content_type, timeout=timeout)

    remote = Remote({})
    payload = remote.search(checksum="abc", features={"stars": 5.0}, raw=b"x", mods="DT")
    assert payload["source"] == "features"
    assert calls["n"] == 1
    assert calls["body"]["feature_version"] == FEATURE_VERSION
    # featurise avec les mods canoniques de la variante, pas les mods bruts
    assert calls["body"]["featurized_with"] == "DT"


def test_un_serveur_d_une_autre_version_recoit_le_fichier_pas_le_vecteur():
    """
    C'est le point de tout le versionnage : plutot que d'envoyer des features
    que le serveur interpreterait de travers, on lui envoie le .osu et il
    featurise avec son propre code.
    """
    seen = {}

    class Remote(FakeRemote):
        def _call(self, path, *, params=None, body=None, content_type="", timeout=None):
            if path == "/api/v1/similar" and body is None:
                raise RemoteError("absent", status=404)
            if path == "/api/v1/similar/file":
                seen["body"] = body
                seen["type"] = content_type
                return _reply("file")
            return super()._call(path, params=params, body=body,
                                 content_type=content_type, timeout=timeout)

    remote = Remote({}, feature_version="0000deadbeef")
    payload = remote.search(checksum="abc", features={"stars": 5.0}, raw=b"osu bytes")
    assert payload["source"] == "file"
    assert seen["body"] == b"osu bytes"
    assert seen["type"] == "application/octet-stream"


def test_sans_rien_a_envoyer_le_404_devient_une_erreur():
    remote = FakeRemote({"/api/v1/similar": RemoteError("absent", status=404)})
    with pytest.raises(RemoteError):
        remote.search(checksum="abc")


def test_une_panne_n_essaie_pas_les_autres_chemins():
    """Un 503 ne veut pas dire que la map est absente : inutile d'insister."""
    remote = FakeRemote({"/api/v1/similar": RemoteError("down", status=503)})
    with pytest.raises(RemoteError):
        remote.search(checksum="abc", features={"stars": 5.0}, raw=b"x")
    assert [call[0] for call in remote.calls] == ["/api/v1/similar"]
    # et on laisse le serveur tranquille un moment
    assert remote.cooling_down


def test_une_erreur_de_requete_ne_met_pas_le_serveur_en_quarantaine():
    remote = FakeRemote({"/api/v1/similar": RemoteError("bad", status=400)})
    with pytest.raises(RemoteError):
        remote.search(checksum="abc")
    assert not remote.cooling_down


@pytest.mark.parametrize("status,attendu", [
    (0, "unreachable"), (403, "blocked by the edge (403)"),
    (409, "feature set mismatch"), (429, "rate limited"), (503, "server error 503"),
])
def test_les_erreurs_ont_un_resume_lisible(status, attendu):
    assert RemoteError("x", status=status).summary() == attendu


def test_un_user_agent_est_toujours_envoye(monkeypatch):
    """
    Sans lui, Cloudflare renvoie 403 : mesure sur l'instance en production,
    `Python-urllib/3.12` est refuse systematiquement.
    """
    captured = {}

    class FakeResponse:
        def read(self):
            return b'{"ok": true}'

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(request, timeout=None):
        captured["ua"] = request.get_header("User-agent")
        return FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    RemoteBackend("https://exemple.test")._call("/api/v1/meta")
    assert captured["ua"] == USER_AGENT
    assert "urllib" not in captured["ua"].lower()


# --- bascule du serveur local ---------------------------------------------------

class SilentWatcher:
    """Un watcher tosu qui ne se connecte a rien : les tests ne dependent pas
    d'osu! ni de tosu tournant sur la machine."""

    def __init__(self, on_map, **kwargs):
        self.on_map = on_map
        self.connected = False
        self.last_error = "tosu absent (test)"
        self.base_url = "http://127.0.0.1:24050"

    async def run(self):
        import asyncio

        await asyncio.Event().wait()


@pytest.fixture
def local_app(mini_index, monkeypatch):
    """Fabrique un serveur local dont le distant est simule."""
    monkeypatch.setattr(server_module, "TosuWatcher", SilentWatcher)

    def build(remote: RemoteBackend | None, mode: str = "remote"):
        monkeypatch.setattr(
            server_module, "RemoteBackend", lambda url, **kw: remote
        )
        return server_module.create_app(
            mini_index, api_url="https://exemple.test", mode=mode
        )

    return build


def _current(checksum: str):
    from osureco.tosu import CurrentMap

    return CurrentMap(
        checksum=checksum, beatmap_id=1, beatmapset_id=1, artist="a", title="t",
        version="v", mapper="m", mode="osu", state="selectPlay", stars=5.0,
        length_s=60.0, bpm=180.0,
    )


def test_le_mode_distant_interroge_le_serveur(local_app, mini_corpus):
    _, features = mini_corpus[0]
    remote = FakeRemote({"/api/v1/similar": _reply("corpus")})
    with TestClient(local_app(remote)) as client:
        server_module.state.current = _current(features["checksum"])
        payload = client.get("/api/similar?k=3").json()
    assert payload["backend"]["active"] == "remote"
    assert payload["source"] == "corpus"
    assert remote.calls[-1][1]["checksum"] == features["checksum"]


def test_un_distant_en_panne_bascule_en_local_et_le_dit(local_app, mini_corpus):
    _, features = mini_corpus[0]
    remote = FakeRemote({"/api/v1/similar": RemoteError("down", status=503)})
    with TestClient(local_app(remote)) as client:
        server_module.state.current = _current(features["checksum"])
        payload = client.get("/api/similar?k=3&star_range=10").json()
        backend = payload["backend"]
        # la page apprend la bascule sans avoir a la deviner
        assert client.get("/api/current").json()["backend"]["active"] == "local"

    assert backend["wanted"] == "remote"
    assert backend["active"] == "local"
    assert backend["fallback_reason"] == "server error 503"
    assert payload["count"] > 0  # et la recherche a bien abouti


def test_sans_index_local_la_panne_distante_est_une_erreur(tmp_path, monkeypatch):
    monkeypatch.setattr(server_module, "TosuWatcher", SilentWatcher)
    remote = FakeRemote({"/api/v1/similar": RemoteError("down", status=503)})
    monkeypatch.setattr(server_module, "RemoteBackend", lambda url, **kw: remote)
    app = server_module.create_app(tmp_path, api_url="https://exemple.test")
    with TestClient(app) as client:
        server_module.state.current = _current("abc")
        response = client.get("/api/similar")
    assert response.status_code == 502
    assert "503" in response.json()["detail"]


def test_la_page_peut_choisir_le_moteur(local_app, mini_corpus):
    _, features = mini_corpus[0]
    remote = FakeRemote({"/api/v1/similar": _reply("corpus")})
    with TestClient(local_app(remote)) as client:
        assert client.post("/api/mode", json={"mode": "local"}).json()["wanted"] == "local"
        server_module.state.current = _current(features["checksum"])
        payload = client.get("/api/similar?k=3&star_range=10").json()
        assert payload["backend"]["active"] == "local"
        assert client.post("/api/mode", json={"mode": "remote"}).json()["wanted"] == "remote"
        assert client.post("/api/mode", json={"mode": "cloud"}).status_code == 400


def test_le_mode_local_est_refuse_sans_index(tmp_path, monkeypatch):
    monkeypatch.setattr(server_module, "TosuWatcher", SilentWatcher)
    remote = FakeRemote({})
    monkeypatch.setattr(server_module, "RemoteBackend", lambda url, **kw: remote)
    app = server_module.create_app(tmp_path, api_url="https://exemple.test")
    with TestClient(app) as client:
        response = client.post("/api/mode", json={"mode": "local"})
    assert response.status_code == 409


def test_sans_index_ni_api_le_serveur_refuse_de_demarrer(tmp_path, monkeypatch):
    monkeypatch.setattr(server_module, "TosuWatcher", SilentWatcher)
    app = server_module.create_app(tmp_path, api_url="", mode="local")
    with pytest.raises(RuntimeError, match="No index"):
        with TestClient(app):
            pass


def test_le_serveur_local_s_ouvre_au_counter_tosu(local_app, mini_corpus):
    """
    Le counter est servi par tosu, donc sur une autre origine. Sans CORS il ne
    pourrait pas utiliser l'index de la machine, et le mode "tout en local"
    n'existerait que dans la page servie par Python.
    """
    remote = FakeRemote({"/api/v1/similar": _reply("corpus")})
    with TestClient(local_app(remote)) as client:
        response = client.get(
            "/api/v1/meta", headers={"Origin": "http://127.0.0.1:24050"}
        )
        assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:24050"

        # ... mais pas a n'importe qui : l'index d'un joueur n'a pas a etre
        # interrogeable par une page qu'il visite
        etranger = client.get(
            "/api/v1/meta", headers={"Origin": "https://exemple-mechant.test"}
        )
        assert "access-control-allow-origin" not in etranger.headers
