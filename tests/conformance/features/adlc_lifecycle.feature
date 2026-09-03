Feature: ADLC end-to-end run lifecycle
  As a repository maintainer using the ADLC framework
  I want a brief to flow through qualify, spec, enrich, graph, build,
  evidence, eval and gate, and be reduced to a canonical run document
  So that I have an auditable, schema-valid record of what happened and why

  Background:
    Given a fresh ADLC-initialised repository
    And the "dark-mode" example brief

  Scenario: A qualifying brief reaches a gated, reduced run
    When the brief is driven through the full ADLC pipeline
    Then the run status is "reported"
    And the run document validates against the "adlc-run" schema
    And the stage history is append-only
    And every declared artifact has a verified sha256 digest

  Scenario: The task graph produced from the brief is acyclic and parallel-safe
    When the brief is driven through the full ADLC pipeline
    Then the task graph has no cycles
    And at least two nodes share a level
    And no two nodes at the same level declare overlapping write sets

  Scenario: A required gate that never ran fails the aggregate
    Given a run that has been driven through the full ADLC pipeline
    When a required gate's status is forced to "not_run"
    Then the aggregate gate status is "fail"
