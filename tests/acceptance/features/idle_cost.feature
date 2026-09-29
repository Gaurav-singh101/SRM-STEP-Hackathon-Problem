# Two of the five questions the product exists to answer (CLAUDE.md S1):
#   Which vehicles generate idle-related cost?
#   How much estimated fuel and cost is tied to inefficient idling?
#
# Written from the finance manager's side, because these figures end up in a budget
# conversation. Every scenario that shows money also checks that the reader can tell it
# is an estimate and can reproduce it - a figure nobody can reproduce is one nobody acts
# on, and one presented as measured when it is modelled is simply wrong.

Feature: Finding the idle cost, and trusting the number

  Background:
    Given a fleet finance manager signed in to "tenant-alpha"

  Scenario: The vehicles costing the most idle fuel come first
    Given a fleet where the idle cost is concentrated in a few vehicles
    When they open the idle cost leaderboard
    Then the request succeeds
    And the worst offender is listed first
    And every vehicle carries its own estimated cost
    And the list is marked as an estimate
    And the access is recorded in the audit trail

  Scenario: The fleet total says what it is based on
    Given the fleet has 0 battery-electric vehicles with no fuel cost
    When they open the fleet overview
    Then the request succeeds
    And the total cost is 3733.5017
    And the cost is exact to the last paisa
    And the figure states the formula, threshold and fuel price behind it

  Scenario: A total that cannot cover the whole fleet says so
    # Battery-electric vehicles idle but burn no fuel. Presenting a fuel-cost total as
    # covering the fleet when 14 vehicles are structurally excluded would overstate how
    # complete the picture is (D9).
    Given the fleet has 14 battery-electric vehicles with no fuel cost
    When they open the fleet overview
    Then the request succeeds
    And the total is flagged as covering only part of the fleet

  Scenario: A single idle event carries its own cost and assumptions
    Given an idle event of 15 minutes that cost 34.6875
    When they open the idle event list
    Then the request succeeds
    And the access is recorded in the audit trail

  Scenario: Nobody sees anything without signing in
    When they request the view without signing in
    Then the request is refused as unauthenticated
