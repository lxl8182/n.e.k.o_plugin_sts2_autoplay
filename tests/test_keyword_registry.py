from plugin.plugins.sts2_autoplay.keyword_registry import build_keyword_effects, reset_cache


def _raw(hand_text=None, powers=None):
    return {
        "combat": {
            "hand": [{"resolved_rules_text": hand_text}] if hand_text else [],
            "player": {"powers": powers or []},
            "enemies": [],
        },
        "run": {},
    }


def _view(hand_text=None):
    return {"screen": "COMBAT", "combat": {"hand": [{"line": hand_text}] if hand_text else []}}


def test_bound_keyword_injected_only_when_hand_has_it():
    reset_cache()
    effects = build_keyword_effects(_raw("防御 [1费]：获得8点格挡。 魂缚"), _view("防御 [1费]：获得8点格挡。 魂缚"))
    assert "魂缚" in effects
    assert "每回合只能打出1张" in effects["魂缚"]

    reset_cache()
    assert build_keyword_effects(_raw(), _view()) == {}


def test_bound_keyword_is_hand_scoped_and_supports_alias():
    reset_cache()
    power_only = _raw(None, [{"name": "魂缚锁链", "description": "每回合抽到的前3张牌会被侵蚀为魂缚。"}])
    assert build_keyword_effects(power_only, _view()) == {}

    reset_cache()
    assert "魂缚" in build_keyword_effects(_raw("Soulbound card"), _view("Soulbound card"))
