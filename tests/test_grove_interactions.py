"""Formal public interfaces and local installations; not real Agent evidence."""
from copy import deepcopy
from dataclasses import replace
import unittest

from agent_world import FunctionOutcome, FunctionSpec, WorldRuntime
from agent_world.errors import PermissionDenied, RuleViolation, StateConflict
from agent_world.simulation import matter as sm
from agent_world.worlds.shared_grove import contact, world_facts, domains
from agent_world.worlds.shared_grove import model as m
from agent_world.worlds.shared_grove.definitions import DEFINITIONS, execution_semantics, material_uses
from agent_world.worlds.shared_grove.world import make_world
import test_grove_creative as fixtures


def installed_definition(key="local_reweave", rules=1):
    return {"key": key, "name": "容器局部重织", "version": 1,
            "types": {"catalog": {"name": "说明器", "body_material": "stone", "cost": {"stone": 1}}},
            "contact_reactions": [{"id": "reweave_" + str(index), "activation": "installed", "when": True,
                "reaction": {"op": "react", "inputs": {"wood": 1}, "outputs": {"cloth": 1}}}
                for index in range(rules)]}


class LocalInteractionTests(unittest.TestCase):
    def setUp(self):
        self.h = fixtures.CreativeWorldTests(methodName="runTest")
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)

    def publish(self, actor="A", key="local_reweave", rules=1):
        return self.h.call(actor, "grove.definition_publish", {"definition": installed_definition(key, rules)})["definition_ref"]

    def add_wood(self, target, amount=1):
        h = self.h
        source = h.item("A", "木柴")["id"]
        lot = next(row for row in h.entity(source)["matter"] if row["material"] == "wood")
        h.interact("A", target, "transfer", item_id=source, lot_id=lot["id"], amount=amount)

    def physical_active(self, target):
        owner = self.h.world.get_state("w", domains.OWNERS, target)
        if not owner or not owner["exists"]:
            return []
        domain = self.h.world.get_state("w", domains.DOMAINS, owner["value"]["domain_id"])
        return domain["value"]["physical_active"]

    def test_installation_is_local_no_stock_and_other_actor_can_continue_public_result(self):
        h = self.h
        reference = self.publish()
        target = h.item("A", "水壶")["id"]
        self.add_wood(target)
        h.ticks(1)
        self.assertEqual(sm.quantity(h.entity(target), "wood", "contained"), 1)
        before = deepcopy(h.entity(target)["matter"])
        result = h.interact("A", target, "install_interaction", definition_ref=reference)
        self.assertFalse(result["action_result"]["quantity_changed"])
        self.assertEqual(h.entity(target)["matter"], before)
        h.ticks(1)
        self.assertEqual(sm.quantity(h.entity(target), "wood", "contained"), 0)
        self.assertEqual(sm.quantity(h.entity(target), "cloth", "contained"), 1)
        # Put the actual vessel in an existing public creation area, using the
        # same movement and drop actions rather than changing its stored owner.
        _, public = h.publish("A", {"key": "public_anchor", "name": "公开锚点", "version": 1,
            "types": {"anchor": {"name": "锚点", "body_material": "stone", "cost": {"stone": 1}}}}, ["anchor"])
        h.approach("A", public["anchor"])
        anchor = h.entity(public["anchor"])
        player = h.view()["player"]
        if (player["x"], player["y"]) != (anchor["x"], anchor["y"]):
            h.call("A", "grove.move", {"dx": anchor["x"]-player["x"], "dy": anchor["y"]-player["y"],
                                      "expected_player_version": player["version"]})
        h.interact("A", target, "drop")
        h.approach("B", target)
        observed = h.read("B", "grove.inspect", {"entity_id": target})["entity"]["simulation"]["contact"]
        self.assertEqual(observed["installed_interactions"][0]["definition_ref"], reference)
        h.interact("B", target, "uninstall_interaction", definition_ref=reference)
        self.assertEqual(sm.quantity(h.entity(target), "cloth", "contained"), 1)
        self.assertNotIn("interaction_definitions", h.entity(target)["state"])

    def test_closed_personal_and_foreign_owned_targets_reject_without_material_change(self):
        h = self.h
        reference = self.publish()
        target = h.item("A", "水壶")["id"]
        before = deepcopy(h.entity(target))
        with self.assertRaises(PermissionDenied):
            h.interact("B", target, "install_interaction", definition_ref=reference)
        self.assertEqual(h.entity(target), before)
        h.interact("A", target, "close")
        closed = deepcopy(h.entity(target))
        with self.assertRaises(RuleViolation):
            h.interact("A", target, "install_interaction", definition_ref=reference)
        self.assertEqual(h.entity(target), closed)
        backpack = h.view()["player"]["backpack_id"]
        with self.assertRaises(RuleViolation):
            h.interact("A", backpack, "install_interaction", definition_ref=reference)

    def test_exact_target_version_duplicate_operation_and_restart_preserve_installation(self):
        h = self.h
        reference = self.publish()
        target = h.item("A", "水壶")["id"]
        args = {"action": "install_interaction", "target_id": target, "definition_ref": reference,
                "expected_target_version": h.entity(target)["version"],
                "expected_player_version": h.view()["player"]["version"]}
        first = h.call("A", "grove.interact", args, operation="one-local-install")
        again = h.call("A", "grove.interact", args, operation="one-local-install")
        self.assertEqual(first, again)
        with self.assertRaises(StateConflict):
            h.call("A", "grove.interact", args)
        self.assertNotIn(target, self.physical_active(target))
        observed = h.read("A", "grove.inspect", {"entity_id": target})["entity"]["simulation"]["contact"]
        self.assertEqual(observed["candidate_count"], 0)
        self.assertEqual(observed["installed_interactions"][0]["definition_ref"], reference)
        h.world = WorldRuntime(h.db)
        h.world.install_world("w", make_world())
        self.assertEqual(h.entity(target)["state"]["interaction_definitions"], [reference])
        self.add_wood(target)
        self.assertIn(target, self.physical_active(target))
        h.ticks(1)
        self.assertEqual(sm.quantity(h.entity(target), "cloth", "contained"), 1)
        self.assertNotIn(target, self.physical_active(target))

    def test_candidate_budget_rejects_complete_install_without_adding_partial_reference(self):
        h = self.h
        first = self.publish(key="first_local", rules=5)
        second = self.publish(key="second_local", rules=4)
        target = h.item("A", "水壶")["id"]
        h.interact("A", target, "install_interaction", definition_ref=first)
        before = deepcopy(h.entity(target))
        with self.assertRaisesRegex(RuleViolation, "候选超过8"):
            h.interact("A", target, "install_interaction", definition_ref=second)
        self.assertEqual(h.entity(target), before)


class CrossAuthorInterfaceTests(unittest.TestCase):
    def setUp(self):
        self.h = fixtures.CreativeWorldTests(methodName="runTest")
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)

    def definitions(self):
        source = {"key": "exported_switch", "name": "公开就绪开关", "version": 1,
            "types": {"source": {"name": "源开关", "body_material": "stone", "cost": {"stone": 1},
                "fields": {"ready": {"type": "boolean", "default": False}}, "exports": ["ready"],
                "actions": [{"id": "set_ready", "label": "设置就绪", "when": True,
                    "parameters": {"enabled": {"type": "boolean", "default": False}},
                    "effects": [{"op": "set", "field": "ready", "value": {"param": "enabled"}}]}]}}}
        reference = self.h.call("A", "grove.definition_publish", {"definition": source})["definition_ref"]
        consumer = {"key": "foreign_interface_gate", "name": "异作者接口门", "version": 1,
            "types": {"observer": {"name": "接口门", "body_material": "stone", "cost": {"stone": 1},
                "fields": {"seen": {"type": "boolean", "default": False}},
                "ports": {"switch": {"source": {"definition_ref": reference, "type_id": "source"},
                                      "fields": ["authored.ready"]}},
                "barrier": {"field": "seen"},
                "rules": [{"id": "follow", "phase": "after", "when": True,
                    "effects": [{"op": "set", "field": "seen", "value": {
                        "object": {"port": "switch", "field": "authored.ready"}}}]}]}}}
        return reference, consumer

    def test_cross_author_typed_port_follows_real_actions_and_preserves_binding_on_restart(self):
        h = self.h
        reference, definition = self.definitions()
        consumer_ref, objects = h.publish("B", definition, ["source", "observer"], refs={"source": reference})
        source, observer = objects["source"], objects["observer"]
        self.assertNotEqual(h.world.get_state("w", DEFINITIONS, reference)["value"]["author"],
                            h.world.get_state("w", DEFINITIONS, consumer_ref)["value"]["author"])
        h.action("B", observer, "bind_port", port="switch", other_id=source)
        h.ticks(1)
        self.assertFalse(h.entity(observer)["state"]["program"]["fields"]["seen"])
        h.action("A", source, "set_ready", params={"enabled": True})
        h.ticks(1)
        self.assertTrue(h.entity(observer)["state"]["program"]["fields"]["seen"])
        self.assertTrue(h.entity(observer)["components"]["barrier"]["active"])
        # A second identity acts on the same source and receives the same world
        # consequence; neither private model context nor a special gate action.
        h.action("B", source, "set_ready", params={"enabled": False})
        h.ticks(1)
        self.assertFalse(h.entity(observer)["components"]["barrier"]["active"])
        saved = deepcopy(h.entity(observer))
        h.world = WorldRuntime(h.db)
        h.world.install_world("w", make_world())
        self.assertEqual(h.entity(observer), saved)
        self.assertEqual(saved["state"]["program"]["port_bindings"], {"switch": source})
        self.assertEqual(saved["state"]["program"]["definition_ref"], consumer_ref)

    def test_future_source_execution_semantics_reject_import_publication_atomically(self):
        h = self.h
        reference, consumer = self.definitions()
        original = deepcopy(h.world.get_state("w", DEFINITIONS, reference)["value"])

        def future_metadata(ctx, args):
            # Represent a stored record from a newer execution policy. This
            # fixture changes no definition payload, hash, object or material.
            row = ctx.get_state_record(DEFINITIONS, reference)
            ctx.set_state(DEFINITIONS, reference, {**row["value"], "execution_semantics": "grove.program.v999"},
                          expected_version=row["version"])
            return FunctionOutcome({})

        definition = make_world()
        fixture = FunctionSpec("grove.future_policy_fixture", future_metadata,
                               {"type": "object", "additionalProperties": False})
        h.world.install_world("w", replace(definition, version=definition.version+1,
                                           functions=(*definition.functions, fixture)))
        h.call("A", "grove.future_policy_fixture", {})
        before_directory = h.read("A", "grove.definition_list", {"limit": 8})
        before_ledger = h.world.get_state("w", m.META, "publication_storage")
        with self.assertRaisesRegex(RuleViolation, "执行语义"):
            h.call("B", "grove.definition_publish", {"definition": consumer})
        self.assertEqual(h.read("A", "grove.definition_list", {"limit": 8}), before_directory)
        self.assertEqual(h.world.get_state("w", m.META, "publication_storage"), before_ledger)
        actual = h.world.get_state("w", DEFINITIONS, reference)["value"]
        self.assertEqual(actual["definition"], original["definition"])
        self.assertEqual(actual["hash"], original["hash"])


class PublicInterfaceTests(unittest.TestCase):
    def setUp(self):
        self.reference = "program_source"
        self.spec = {"fields": {"ready": {"type": "boolean", "default": False},
                                "pressure": {"type": "integer", "default": 0, "min": 0, "max": 9},
                                "private": {"type": "integer", "default": 2, "min": 0, "max": 9}},
                     "exports": ["ready", "pressure"]}
        record = {"definition": {"materials": {}, "types": {"source": self.spec}}}
        self.ctx = type("Context", (), {"get_state": lambda _, scope, key: record if key == self.reference else None})()
        self.entity = {"state": {"program": {"definition_ref": self.reference, "type_id": "source",
                                               "fields": {"ready": True, "pressure": 3, "private": 2}}}}
        self.port = {"source": {"definition_ref": self.reference, "type_id": "source"},
                     "fields": ["authored.ready", "authored.pressure"]}

    def test_only_actual_typed_exports_are_public_and_wrong_source_does_not_match(self):
        self.assertEqual(world_facts.interface_facts(self.ctx, self.entity),
                         {"authored.ready": True, "authored.pressure": 3})
        self.assertTrue(world_facts.validate_port_source(self.ctx, self.entity, self.port))
        other = deepcopy(self.entity)
        other["state"]["program"]["type_id"] = "other"
        self.assertFalse(world_facts.validate_port_source(self.ctx, other, self.port))
        hidden = deepcopy(self.port)
        hidden["fields"] = ["authored.private"]
        self.assertFalse(world_facts.validate_port_source(self.ctx, self.entity, hidden))

    def test_invalid_values_are_omitted_not_replaced_with_defaults(self):
        self.entity["state"]["program"]["fields"] = {"ready": 1, "pressure": 10}
        self.assertEqual(world_facts.interface_facts(self.ctx, self.entity), {})
        self.assertFalse(world_facts.validate_port_source(self.ctx, self.entity, {"fields": ["authored.ready"]}))

    def test_old_execution_policy_is_explicitly_compatible_and_future_policy_is_not_reinterpreted(self):
        self.assertEqual(execution_semantics({}), "grove.program.v1")
        self.assertEqual(execution_semantics({"execution_semantics": "grove.program.v1"}), "grove.program.v1")
        with self.assertRaises(RuleViolation):
            execution_semantics({"execution_semantics": "grove.program.v999"})

    def test_future_material_policy_cannot_grant_current_fuel_permissions(self):
        record = {"definition": {"materials": {"coal": {"uses": {"mechanical_fuel": True}}}}}
        ctx = type("Context", (), {"get_state": lambda _, scope, key: record if key == "future" else None})()
        self.assertTrue(material_uses(ctx, "future#coal")["mechanical_fuel"])
        record["execution_semantics"] = "grove.program.v999"
        with self.assertRaisesRegex(RuleViolation, "执行语义"):
            material_uses(ctx, "future#coal")

    def test_exports_remain_hidden_for_closed_private_or_closed_ancestor_snapshots(self):
        entity = m.normalize_entity({"id": "source_object", "name": "源", "material": "stone",
            "x": 9, "y": 9, "capabilities": ["transport", "container"],
            "state": {"program": deepcopy(self.entity["state"]["program"])}})
        objects = {entity["id"]: entity}
        self.assertEqual(world_facts.facts(entity, objects, external=True, ctx=self.ctx)["authored.pressure"], 3)
        entity["state"]["open"] = False
        self.assertNotIn("authored.pressure", world_facts.facts(entity, objects, external=True, ctx=self.ctx))
        entity["state"]["open"] = True
        entity["owner"] = "private_owner"
        self.assertNotIn("authored.ready", world_facts.facts(entity, objects, external=True, ctx=self.ctx))
        entity["owner"] = None
        parent = m.normalize_entity({"id": "closed_parent", "name": "外容器", "material": "stone",
            "x": 9, "y": 9, "capabilities": ["transport", "container"], "state": {"open": False}})
        entity["physical_parent"] = {"id": parent["id"], "relation": "contained"}
        parent["state"]["contents"] = [entity["id"]]
        objects[parent["id"]] = parent
        self.assertNotIn("authored.ready", world_facts.facts(entity, objects, external=True, ctx=self.ctx))

    def test_installed_reference_shape_is_bounded_without_global_lookup(self):
        self.assertEqual(contact.installed_references({"state": {}}), [])
        for references in (["a", "a"], ["a", "b", "c"], "a", [0]):
            with self.subTest(references=references), self.assertRaises(RuleViolation):
                contact.installed_references({"state": {"interaction_definitions": references}})
