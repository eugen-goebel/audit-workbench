"""Role graph view, SoD highlight and role lookup."""

from __future__ import annotations

import math

import networkx as nx
import pytest

from audit_workbench.dashboard import views
from audit_workbench.runner import Run


def test_graph_view_has_the_nodes_and_edges_of_the_role_graph(run7: Run) -> None:
    view = views.role_graph_view(run7.access)
    graph = run7.access.role_graph
    assert (len(view.nodes), len(view.edges)) == (39, 29)
    assert (len(view.nodes), len(view.edges)) == (graph.number_of_nodes(), graph.number_of_edges())
    assert view.nodes["kind"].value_counts().to_dict() == {"role": 22, "entitlement": 17}
    assert set(zip(view.edges["source"], view.edges["target"], strict=True)) == set(graph.edges)


def test_graph_nodes_have_distinct_finite_positions_and_labels(run7: Run) -> None:
    nodes = views.role_graph_view(run7.access).nodes
    assert nodes["node"].is_unique
    assert all(math.isfinite(v) for v in nodes["x"]) and all(math.isfinite(v) for v in nodes["y"])
    assert not nodes.duplicated(["x", "y"]).any()
    assert (nodes["name"].str.len() > 0).all()


def test_graph_layers_follow_the_edge_direction(run7: Run) -> None:
    view = views.role_graph_view(run7.access)
    layer = dict(zip(view.nodes["node"], view.nodes["layer"], strict=True))
    assert all(
        layer[s] < layer[t] for s, t in zip(view.edges["source"], view.edges["target"], strict=True)
    )


def test_sod_highlight_marks_both_paths_and_the_accounts(run7: Run) -> None:
    graph = run7.access.role_graph
    a05 = [f for f in run7.findings if f.rule_id == "A-05"]
    assert len(a05) == 9
    for finding in a05:
        highlight = views.sod_highlight(run7.access, finding)
        assert set(highlight.paths) == set(finding.details) and len(highlight.paths) == 2
        assert highlight.accounts == sorted({p[0] for p in highlight.paths.values()})
        assert highlight.nodes <= set(graph.nodes)
        assert highlight.edges <= set(graph.edges)
        for fn, path in highlight.paths.items():
            assert path[-1] in graph and graph.nodes[path[-1]]["kind"] == "entitlement"
            assert run7.access.entitlements.at[path[-1], "business_function"] == fn
            assert set(path[1:]) <= highlight.nodes
            assert set(zip(path[1:], path[2:], strict=False)) <= highlight.edges


def test_sod_highlight_rejects_other_rules(run7: Run) -> None:
    other = next(f for f in run7.findings if f.rule_id == "A-01")
    with pytest.raises(ValueError, match="A-05"):
        views.sod_highlight(run7.access, other)


def test_role_info_lists_accounts_and_everything_granted(run7: Run) -> None:
    access = run7.access
    graph = access.role_graph
    manager = "R-GL-MANAGER"
    info = views.role_info(access, manager)
    assert info.role_id == manager and info.name and info.system == "GL"
    assert info.grants_roles == sorted(access.roles_within(manager) - {manager})
    assert "R-GL-CLERK" in info.grants_roles
    want = {n for n in nx.descendants(graph, manager) if graph.nodes[n]["kind"] == "entitlement"}
    assert set(info.entitlements["entitlement_id"]) == want
    assert {"entitlement_id", "name", "business_function"} <= set(info.entitlements.columns)


def test_role_info_accounts_are_the_active_holders(run7: Run) -> None:
    access = run7.access
    role = "R-GL-CLERK"
    info = views.role_info(access, role)
    containers = {role} | nx.ancestors(access.role_graph, role)
    active = access.active_assignments
    want = set(active.loc[active["role_id"].isin(containers), "account_id"])
    assert set(info.accounts["account_id"]) == want and want
    assert set(info.accounts["via_role"]) <= containers
    direct = set(active.loc[active["role_id"] == role, "account_id"])
    assert direct <= set(info.accounts.loc[info.accounts["via_role"] == role, "account_id"])


def test_role_info_unknown_role(run7: Run) -> None:
    with pytest.raises(KeyError):
        views.role_info(run7.access, "R-NOPE")
