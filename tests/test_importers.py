import json

from floorball_bot.importers import read_city_bundle, reconcile


def write_bundle(path, players=1):
    path.write_text(
        json.dumps(
            {
                "ok": True,
                "version": 1,
                "generatedAt": "2026-08-24T00:00:00Z",
                "cities": [
                    {
                        "slug": "almaty",
                        "nameRu": "Алматы",
                        "nameKz": "Алматы",
                        "nameEn": "Almaty",
                        "updatedAt": "2026-08-24T00:00:00Z",
                        "players": players,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_google_payload_import_is_hash_idempotent(tmp_path):
    source = tmp_path / "cities.json"
    write_bundle(source)
    first = read_city_bundle(source)
    second = read_city_bundle(source)
    assert first == second
    report = reconcile({"almaty": first[0].content_hash}, second)
    assert report["unchanged"] == ["almaty"]
    assert report["create"] == []


def test_reconciliation_detects_update(tmp_path):
    source = tmp_path / "cities.json"
    write_bundle(source, players=2)
    record = read_city_bundle(source)[0]
    report = reconcile({"almaty": "0" * 64, "astana": "1" * 64}, [record])
    assert report["update"] == ["almaty"]
    assert report["missing_from_source"] == ["astana"]
