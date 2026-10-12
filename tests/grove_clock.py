"""Drive complete saved world frames, without replacing their actual scheduler."""
from agent_world.worlds.shared_grove import model as m


def advance_frames(case, runtime, universe, clock, count):
    for _ in range(count):
        before = runtime.get_state(universe, m.META, "clock")["value"]
        target = before.get("completed_sequence", before["sequence"]) + 1
        clock.return_value += 1
        processed = []
        for _ in range(512):
            result = runtime.run_due_timers(universe)
            processed.extend(result["processed"])
            current = runtime.get_state(universe, m.META, "clock")["value"]
            if current.get("completed_sequence", current["sequence"]) >= target:
                case.assertEqual(current["sequence"], target, processed)
                break
            if not result["count"]:
                # A rejected child has no success wake; discover its saved
                # terminal receipt through the coordinator's bounded poll.
                clock.return_value += .1
        else:
            case.fail(f"saved global frame did not finish: {processed}")
        case.assertTrue(processed)
        case.assertFalse([row for row in processed if row["status"] in {"failed", "blocked"}], processed)

