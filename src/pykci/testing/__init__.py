"""Test support of pykci's suite:

  database   a throwaway Neo4j for integration tests, with the guards that keep a test
             from ever wiping a real graph
  plugin     the pytest plugin: markers and fixtures (`pytest_plugins = ["pykci.testing.plugin"]`)
  clones     repeated code: identical function bodies and module-level tables across files
  commands   the contract of a command table (importable, --help, documented)
  connection the rule that the database is reached through pykci.runtime only
  coverage   which modules the tests name: the rule that the suite grows with the code
  cypher     labels and relationship types named in the Cypher of a code base
  docs       which names the documents of a repository mention, to compare with the code
  lint       names that are used but defined nowhere (what moving code leaves behind)
  snapshot   recorded results (the graph after an ingest, a schema) and how to update them

Nothing here is imported by the pipeline; pytest is only needed by `plugin`.
"""
