# The third product question (CLAUDE.md S1):
#   Which vehicles show low or abnormal utilisation?
#
# The hard part is not the ranking, it is not lying about what the ranking covers. Two
# failures found during development are pinned here as scenarios: a vehicle with no
# telemetry must never be reported as under-used, and a page listing the few vehicles that
# could be scored must say so rather than reading as the whole fleet.

Feature: Finding under-used vehicles without mistaking silence for idleness

  Background:
    Given a fleet operations manager signed in to "tenant-alpha"

  Scenario: The under-used vehicles are on the first page
    Given the utilisation scores are
      | score | band     | active_days | moving | distance | reason |
      | 74.0  | normal   | 0.80        | 0.76   | 0.79     |        |
      | 12.5  | low      | 0.18        | 0.14   | 0.20     |        |
      | 45.0  | normal   | 0.50        | 0.48   | 0.52     |        |
    When they open the utilisation view
    Then the request succeeds
    And the under-used vehicles appear before the healthy ones
    And each vehicle shows the three components behind its score
    And the access is recorded in the audit trail

  Scenario: A vehicle whose own signals disagree is reported separately, not as low
    # Out almost every day but barely moving is a data-quality or misuse signal, not a
    # level of use. Counting it as low utilisation would send someone to sell a vehicle
    # over what is probably a broken odometer feed.
    Given the utilisation scores are
      | score | band     | active_days | moving | distance | reason                                     |
      | 31.0  | abnormal | 0.95        | 0.11   | 0.14     | out almost every day but barely moving     |
      | 22.0  | low      | 0.20        | 0.18   | 0.21     |                                            |
    When they open the utilisation view
    Then the request succeeds
    And the abnormal vehicle explains itself in words

  Scenario: Filtering to one band returns only that band
    Given the utilisation scores are
      | score | band   | active_days | moving | distance | reason |
      | 18.0  | low    | 0.20        | 0.18   | 0.22     |        |
      | 66.0  | normal | 0.70        | 0.68   | 0.72     |        |
    When they filter the utilisation view to the "low" band
    Then the request succeeds
    And only vehicles in the "low" band are listed

  Scenario: The page says how much of the fleet it could actually score
    # The failure this prevents: the batch job could not score 1,808 vehicles and the page
    # said nothing, so 180 rows read as a complete picture of a 2,000-vehicle fleet.
    Given the utilisation scores are
      | score | band | active_days | moving | distance | reason |
      | 15.0  | low  | 0.16        | 0.15   | 0.18     |        |
    And 180 of 2000 active vehicles were scored, 1808 having no telemetry and 12 in a class too small
    When they open the utilisation view
    Then the request succeeds
    And the view reports that 180 of 2000 vehicles were scored
    And vehicles with no telemetry are counted separately from unused ones

  Scenario: Nothing scored yet is not the same as everything scored
    Given no utilisation window has been scored yet
    When they open the utilisation view
    Then the request succeeds
    And the view does not claim full coverage
