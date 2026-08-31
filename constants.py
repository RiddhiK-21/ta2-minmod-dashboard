import os
import yaml

# Load the YAML configuration
with open("critical_minerals.yaml", "r") as file:
    config = yaml.safe_load(file)

minerals = set(mineral.lower() for mineral in config["CRITICAL_MINERALS"])
ree_minerals = list(set(mineral.lower().capitalize() for mineral in config["REE"]))
pge_minerals = list(set(mineral.lower().capitalize() for mineral in config["PGE"]))
heavy_ree_minerals = list(
    set(mineral.lower().capitalize() for mineral in config["HEAVY_REE"])
)
light_ree_minerals = list(
    set(mineral.lower().capitalize() for mineral in config["LIGHT_REE"])
)

CRITICAL_MINERALS = minerals.union(ree_minerals)
SPARQL_ENDPOINT = os.environ.get("SPARQL_ENDPOINT", "https://minmod.isi.edu/sparql")
API_ENDPOINT = os.environ.get("API_ENDPOINT", "https://minmod.isi.edu/api/v1")
FRONTEND_ENDPOINT = os.environ.get("FRONTEND_ENDPOINT", "https://minmod.isi.edu") #added to run code locally, remove later

#geochem
GEOCHEM_SPARQL_ENDPOINT = os.environ.get("GEOCHEM_SPARQL_ENDPOINT", "http://dev.minmod.isi.edu:3030/minmod/sparql")
GEOCHEM_PG_DSN = os.environ.get("GEOCHEM_PG_DSN", "postgresql://geochem:geochem@localhost:5432/geochem")