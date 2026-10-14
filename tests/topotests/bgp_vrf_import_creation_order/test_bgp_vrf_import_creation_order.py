#!/usr/bin/env python
# SPDX-License-Identifier: ISC

"""Exercise VRF imports across configuration and kernel VRF creation orders."""

import json
from functools import partial
from pathlib import Path

import pytest

from lib import topotest
from lib.topogen import Topogen

pytestmark = [pytest.mark.bgpd]


def build_topo(tgen):
    r1 = tgen.add_router("r1")
    r2 = tgen.add_router("r2")
    tgen.add_link(r1, r2)


@pytest.fixture(scope="module")
def tgen(request):
    tgen = Topogen(build_topo, request.module.__name__)
    tgen.start_topology()
    for router in tgen.routers().values():
        router.load_frr_config()
    tgen.start_router()
    yield tgen
    tgen.stop_topology()


def expect_json(router, command, expected):
    check = partial(topotest.router_json_cmp, router, command, expected)
    _, result = topotest.run_and_expect(check, None, count=60, wait=0.5)
    assert result is None, result


def create_linux_vrf(tgen, name, table):
    router = tgen.gears["r1"]
    router.cmd_raises("ip link add {} type vrf table {}".format(name, table))
    router.cmd_raises("ip link set {} up".format(name))
    vrf_id = json.loads(router.cmd_raises("ip -j link show {}".format(name)))[0][
        "ifindex"
    ]

    # Wait for bgpd itself to consume the enable event before promoting the
    # BGP instance. Waiting only for Zebra can race with its notification to
    # bgpd and accidentally exercise the working explicit-before-Linux order.
    log = Path(tgen.logdir) / "r1" / "bgpd.log"
    message = "VRF enable add {} id {}".format(name, vrf_id)
    _, result = topotest.run_and_expect(
        lambda: message in log.read_text(), True, count=60, wait=0.5
    )
    assert result, "bgpd did not receive the VRF-enable event for {}".format(name)
    return vrf_id


def configure_import(router, name):
    router.vtysh_cmd(
        """
configure terminal
router bgp 64512
 address-family ipv4 unicast
  import vrf {name}
 exit-address-family
 address-family ipv6 unicast
  import vrf {name}
 exit-address-family
end
""".format(
            name=name
        ),
        raises=True,
    )


def configure_explicit(router, name):
    router.vtysh_cmd(
        """
configure terminal
router bgp 64512 vrf {name}
 address-family ipv4 unicast
  import vrf default
 exit-address-family
 address-family ipv6 unicast
  import vrf default
 exit-address-family
end
""".format(
            name=name
        ),
        raises=True,
    )


def expect_imports(router, name, vrf_id):
    for family, prefix in (
        ("ipv4", "203.0.113.0/24"),
        ("ipv6", "2001:db8:100::/64"),
    ):
        expect_json(
            router,
            "show bgp vrf {} {} unicast json".format(name, family),
            {"vrfId": vrf_id, "routes": {prefix: [{"valid": True}]}},
        )


@pytest.mark.parametrize(
    "name,table,order",
    [
        ("import-first", 1001, ("import", "linux", "explicit")),
        ("linux-first", 1002, ("linux", "import", "explicit")),
        ("explicit-first", 1003, ("explicit", "linux", "import")),
        ("import-explicit", 1004, ("import", "explicit", "linux")),
    ],
)
def test_vrf_binding(tgen, name, table, order):
    """Import both families regardless of creation order and retain bindings."""
    if tgen.routers_have_failure():
        pytest.skip(tgen.errors)

    router = tgen.gears["r1"]
    peer_command = "show bgp neighbors 192.0.2.2 json"
    expect_json(router, peer_command, {"192.0.2.2": {"bgpState": "Established"}})
    peer_before = router.vtysh_cmd(peer_command, isjson=True)["192.0.2.2"]

    for step in order:
        if step == "import":
            configure_import(router, name)
        elif step == "linux":
            vrf_id = create_linux_vrf(tgen, name, table)
        else:
            configure_explicit(router, name)
    expect_imports(router, name, vrf_id)

    # Re-entering the router node must be harmless once the instance is bound.
    configure_explicit(router, name)
    router.vtysh_cmd(
        """
configure terminal
router bgp 64512 vrf {}
 bgp router-id 192.0.2.10
end
""".format(
            name
        ),
        raises=True,
    )
    expect_imports(router, name, vrf_id)

    # Exercise the existing disable/enable callbacks with a new kernel VRF ID.
    router.cmd_raises("ip link del {}".format(name))
    for family in ("ipv4", "ipv6"):
        expect_json(
            router,
            "show bgp vrf {} {} unicast json".format(name, family),
            {"vrfId": -1},
        )
    new_id = create_linux_vrf(tgen, name, table)
    assert new_id != vrf_id
    expect_imports(router, name, new_id)

    expect_json(
        router,
        peer_command,
        {
            "192.0.2.2": {
                "bgpState": "Established",
                "connectionsEstablished": peer_before["connectionsEstablished"],
                "connectionsDropped": peer_before["connectionsDropped"],
            }
        },
    )


def test_memory_leak(tgen):
    if not tgen.is_memleak_enabled():
        pytest.skip("Memory leak test/report is disabled")
    tgen.report_memory_leaks()
