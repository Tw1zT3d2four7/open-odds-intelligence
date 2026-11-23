from app import main


def test_public_events_reuses_sanitized_copy_for_same_snapshot(monkeypatch):
    source = [{
        "id": "event-1",
        "bookmakers": [{
            "key": "book-a",
            "markets": [{
                "key": "h2h",
                "outcomes": [{
                    "name": "Home",
                    "price": -110,
                    "source_provider": "private-provider",
                    "size_source_provider": "private-provider",
                }],
            }],
        }],
    }]
    calls = []

    def events(sport=None):
        calls.append(sport)
        return source

    monkeypatch.setattr(main.service, "events", events)
    main._PUBLIC_EVENTS_CACHE.clear()

    first = main._public_events()
    second = main._public_events()

    assert first is second
    assert len(calls) == 2
    outcome = first[0]["bookmakers"][0]["markets"][0]["outcomes"][0]
    assert outcome == {"name": "Home", "price": -110}


def test_public_events_rebuilds_sanitized_copy_after_snapshot_changes(monkeypatch):
    first_source = [{"id": "event-1", "provider": "private", "bookmakers": []}]
    second_source = [{"id": "event-2", "provider": "private", "bookmakers": []}]
    current = [first_source]

    monkeypatch.setattr(main.service, "events", lambda sport=None: current[0])
    main._PUBLIC_EVENTS_CACHE.clear()

    first = main._public_events()
    current[0] = second_source
    second = main._public_events()

    assert first is not second
    assert first[0]["id"] == "event-1"
    assert second == [{"id": "event-2", "bookmakers": []}]
