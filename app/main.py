from fastapi import FastAPI

app = FastAPI()


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/grid")
def grid():
    # Mock data for now; later this calls the team's code in /src
    return {
        "data_as_of": "2026-11-07T18:00:00Z",
        "cached": True,
        "carbon_intensity": 145,
        "mix_percent": {
            "wind": 42, "gas": 25, "nuclear": 15, "solar": 3,
            "imports": 8, "biomass": 5, "other": 2,
        },
    }
