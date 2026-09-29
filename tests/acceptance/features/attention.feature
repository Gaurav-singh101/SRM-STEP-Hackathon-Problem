# The fourth and fifth product questions (CLAUDE.md S1):
#   Which events or vehicles need operational attention?
#   What are the fleet-level fuel, idle and utilisation trends?
#
# The latency scenarios here exist because of a real mistake. A first run reported an alert
# latency of 83,711 seconds against a 5-second target. Nothing was slow: the pipeline was
# replaying ten days of history, and the figure measured how old the data was rather than
# how fast the system reacted. Both figures are now reported, and the flag says which one
# means anything.

Feature: Seeing what needs attention, with a latency figure that means something

  Background:
    Given a fleet operations manager signed in to "tenant-alpha"

  Scenario: The queue holds only what has not been handled
    Given the attention list holds
      | type            | severity | acknowledged |
      | excessive_idle  | warning  | no           |
      | excessive_idle  | critical | no           |
      | excessive_idle  | warning  | yes          |
    When they open the attention list
    Then the request succeeds
    And 2 alerts need attention
    And each alert carries the detail needed to act on it
    And the access is recorded in the audit trail

  Scenario: Handled alerts can still be reviewed on request
    Given the attention list holds
      | type            | severity | acknowledged |
      | excessive_idle  | warning  | no           |
      | excessive_idle  | warning  | yes          |
    When they review the attention list including handled alerts
    Then the request succeeds
    And 2 alerts need attention

  Scenario: A live alert reports how fast the system reacted
    Given an alert raised from live telemetry 2.86 seconds after the event
    When they open the attention list
    Then the request succeeds
    And the reaction time is reported as 2.86 seconds
    And the alert is not flagged as replayed

  Scenario: A replayed alert does not pass its data age off as a reaction time
    Given an alert raised from a backfill of 24 hours of history
    When they open the attention list
    Then the request succeeds
    And the alert is flagged as coming from replayed history
    And the data age is not presented as the reaction time

  Scenario: The fleet trend can be read as a direction
    When they open the fleet trend
    Then the request succeeds
    And the trend has one point per day
    And every point carries both idle hours and cost
