"""pykci: Python Knowledge Graph for Cities (CityGML <-> Neo4j, OSM fusion, 3D Tiles).

A library and a command line. Every pipeline step is a module of this package that can be
imported (`from pykci.ingest_citygml import ingest`) or run (`pykci ingest-citygml …`,
`python -m pykci.ingest_citygml …`); see pykci.cli for the command table.

Importing the package imports nothing else: each module pulls in only what it needs, so a
consumer that reads provenance rules does not load the CityGML codec.
"""

__version__ = "0.2.0"
