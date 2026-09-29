# Cross-cutting requirements (PDF S11, CLAUDE.md S10 and S11). These are not a separate
# product question; they are the conditions under which the answers to the other four can
# be shown to anyone at all.
#
# Tenant isolation and location privacy are tested here from the outside, as a user would
# meet them. The policies themselves are tested against a real PostgreSQL in
# tests/integration/test_tenant_isolation.py - both matter, because a route that scopes
# correctly over a database that does not is one bug away from a leak.

Feature: The conditions under which fleet data may be shown at all

  Scenario: One tenant cannot reach another tenant's vehicle
    # Not-found rather than forbidden: a 403 would confirm the VIN exists somewhere, which
    # is itself a disclosure across the tenant boundary.
    Given a vehicle "ZZAAAAAAAAAAAAAAA" belonging to "tenant-bravo"
    And a fleet operations manager signed in to "tenant-alpha"
    When they look up vehicle "ZZAAAAAAAAAAAAAAA"
    Then the vehicle is reported as not found

  Scenario: A tenant can reach its own vehicle
    Given a vehicle "ZZAAAAAAAAAAAAAAA" belonging to "tenant-alpha"
    And a fleet operations manager signed in to "tenant-alpha"
    When they look up vehicle "ZZAAAAAAAAAAAAAAA"
    Then the request succeeds
    And the access is recorded in the audit trail

  Scenario: A finance manager sees the cost, not where the driver parked
    Given a fleet finance manager signed in to "tenant-alpha"
    And an idle event recorded at a precise location
    When they open the idle event list
    Then the request succeeds
    And the location is coarsened

  Scenario: An operations manager needs the precise location to act on it
    Given a fleet operations manager signed in to "tenant-alpha"
    And an idle event recorded at a precise location
    When they open the idle event list
    Then the request succeeds
    And the location is precise
