import pytest

from floorball_bot.city_applications import (
    CityApplicationRejected,
    canonical_city_slug,
    load_city_proposal_spec,
    parse_city_proposal_answer,
)
from floorball_bot.dialogue.evaluator import evaluate_gaps


def test_city_proposal_spec_has_all_release_requirement_groups():
    loaded = load_city_proposal_spec()
    report = evaluate_gaps(loaded.spec, {})

    assert report.required_to_start
    assert report.required_for_submit
    assert report.required_for_publish
    assert any(field.requirement.value == "optional" for field in loaded.spec.fields)
    assert len(loaded.sha256) == 64


def test_city_slug_is_deterministic_for_russian_and_kazakh_names():
    assert canonical_city_slug("Кызылорда") == "kyzylorda"
    assert canonical_city_slug("Қонаев") == "qonaev"


def test_city_answer_validation_rejects_bad_consent_url_and_coordinates():
    spec = load_city_proposal_spec().spec
    fields = {field.id: field for field in spec.fields}

    assert parse_city_proposal_answer(fields["accuracy_confirmed"], "да") is True
    with pytest.raises(CityApplicationRejected):
        parse_city_proposal_answer(fields["source_url"], "http://example.kz")
    with pytest.raises(CityApplicationRejected):
        parse_city_proposal_answer(fields["coordinates"], "300, 95")
