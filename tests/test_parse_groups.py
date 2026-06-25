import pandas as pd
import pytest

from libby_core.parse_groups import count_groups, explode_groups, parse_groups


@pytest.mark.parametrize("value", [None, "", "   ", pd.NA, float("nan")])
def test_parse_groups_returns_empty_list_for_empty_values(value):
    assert parse_groups(value) == []


def test_parse_groups_parses_stringified_list_of_group_dicts():
    value = (
        "["
        "{'url': 'https://www.facebook.com/groups/1/', 'name': 'One', "
        "'details': 'Public · 1.2K members'}, "
        "{'url': 'https://www.facebook.com/groups/2/', 'name': 'Two', "
        "'details': 'Private · 15 members'}"
        "]"
    )

    groups = parse_groups(value)

    assert groups == [
        {
            "url": "https://www.facebook.com/groups/1/",
            "name": "One",
            "details": "Public · 1.2K members",
            "privacy": "Public",
            "member_count": 1200,
            "posts_per_day": None,
        },
        {
            "url": "https://www.facebook.com/groups/2/",
            "name": "Two",
            "details": "Private · 15 members",
            "privacy": "Private",
            "member_count": 15,
            "posts_per_day": None,
        },
    ]


def test_parse_groups_accepts_already_parsed_lists():
    value = [{"url": "https://www.facebook.com/groups/1/", "name": "One"}]

    assert parse_groups(value) == [
        {
            "url": "https://www.facebook.com/groups/1/",
            "name": "One",
            "privacy": None,
            "member_count": None,
            "posts_per_day": None,
        }
    ]


def test_parse_groups_extracts_privacy_members_and_daily_posts():
    value = (
        "["
        "{'url': 'https://www.facebook.com/groups/1/', 'name': 'One', "
        "'details': 'Public · 9.3K members · 70+ posts a day'}"
        "]"
    )

    assert parse_groups(value)[0] == {
        "url": "https://www.facebook.com/groups/1/",
        "name": "One",
        "details": "Public · 9.3K members · 70+ posts a day",
        "privacy": "Public",
        "member_count": 9300,
        "posts_per_day": 70,
    }


def test_explode_groups_handles_surrogate_escaped_names():
    df = pd.DataFrame(
        {
            "groups": [
                r'[{"url": "one", "name": "Vote \ud83d\uddf3\ufe0f", "details": "Public · 1 member"}]'
            ],
        }
    )

    exploded = explode_groups(df)

    assert exploded.loc[0, "name"] == "Vote \U0001f5f3\ufe0f"
    assert exploded.loc[0, "member_count"] == 1


@pytest.mark.parametrize(
    "value",
    [
        "not a list",
        "{'url': 'https://www.facebook.com/groups/1/'}",
        "123",
        123,
        ["not a dict"],
    ],
)
def test_parse_groups_returns_empty_list_for_malformed_or_non_list_values(value):
    assert parse_groups(value) == []


def test_count_groups_counts_only_group_dicts():
    value = "[{'url': 'one'}, {'url': 'two'}, 'not a dict']"

    assert count_groups(value) == 2


def test_explode_groups_returns_one_row_per_group_with_group_columns():
    df = pd.DataFrame(
        {
            "place_name": ["Place A", "Place B"],
            "groups": [
                "[{'url': 'one', 'name': 'Group One'}, {'url': 'two', 'name': 'Group Two'}]",
                "[]",
            ],
        }
    )

    exploded = explode_groups(df)

    assert list(exploded["place_name"]) == ["Place A", "Place A"]
    assert list(exploded["url"]) == ["one", "two"]
    assert list(exploded["name"]) == ["Group One", "Group Two"]
    assert "groups_list" in exploded.columns
