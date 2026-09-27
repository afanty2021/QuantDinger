from app.services.strategy_params import canonical_strategy_param_schema


def test_code_param_defaults_replace_stale_saved_schema_values():
    schema = {
        "params": [
            {
                "name": "kc_length",
                "type": "integer",
                "default": 20,
                "min": 2,
                "max": 200,
                "step": 1,
            },
            {
                "name": "entry_pct",
                "type": "percent",
                "default": 0.5,
                "min": 0,
                "max": 1,
            },
            {"name": "removed_param", "type": "number", "default": 99},
        ]
    }
    code = """
# @param kc_length int 50 Keltner length
# @param entry_pct float 0.8 Entry allocation
def initialize(context):
    pass
"""

    result = canonical_strategy_param_schema(code, schema)

    assert [item["name"] for item in result["params"]] == ["kc_length", "entry_pct"]
    assert result["params"][0] == {
        "name": "kc_length",
        "type": "integer",
        "default": 50,
        "min": 2,
        "max": 200,
        "step": 1,
        "description": "Keltner length",
        "source": "code_param",
    }
    assert result["params"][1]["type"] == "percent"
    assert result["params"][1]["default"] == 0.8


def test_code_param_schema_is_created_for_all_supported_types():
    code = """
# @param period int 12 Period
# @param threshold float 1.25 Threshold
# @param enabled bool true Enabled
# @param mode str trend Mode
"""

    result = canonical_strategy_param_schema(code)

    assert [(item["name"], item["type"], item["default"]) for item in result["params"]] == [
        ("period", "integer", 12),
        ("threshold", "number", 1.25),
        ("enabled", "boolean", True),
        ("mode", "text", "trend"),
    ]


def test_schema_without_code_declarations_is_preserved():
    schema = {"params": [{"name": "legacy", "default": 7}]}

    assert canonical_strategy_param_schema("def initialize(context):\n    pass", schema) == schema


def test_code_range_declaration_becomes_numeric_bounds_and_step():
    result = canonical_strategy_param_schema(
        "# @param threshold float 0.02 range=0:0.2:0.005\n"
        "# @param period int 5 range=2:60:2\n"
    )

    threshold, period = result["params"]
    assert (threshold["min"], threshold["max"], threshold["step"]) == (0.0, 0.2, 0.005)
    assert (period["min"], period["max"], period["step"]) == (2, 60, 2)
