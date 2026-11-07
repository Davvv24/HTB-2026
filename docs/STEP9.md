# Step 9 — Explainability

Every appliance, EV and battery plan now includes a computed `why` sentence in `to_dict()`. The sentence uses the plan's expected cost and emissions compared with its baseline, including cases where a recommendation costs more or emits more. Battery comparisons include degradation and use the same household load without a battery as their baseline.

The detailed explanation module also exports model-wide feature importance and local feature contributions using LightGBM's native `pred_contrib=True`. This uses the installed LightGBM package and requires no additional SHAP dependency.

## Run

From the repository root with the project dependencies installed, run these commands.

```powershell
python -m src.explain
python -m src.optimiser_demo --offline --appliance washing_machine --json
python -m unittest discover -s tests -v
```

The first command uses the saved Step 8 models, calibrated forecast table and Step 3 features. It selects the latest saved issuance, 3 November 2026 at 15:00 UTC, and labels the output as a historical demo. It generates recommendations for the three configured appliances, a 20 kWh EV charge, and the battery. Appliance and EV deadlines are next 07:00 UK time. The battery uses the supplied 48-hour window. Example settings use carbon weight zero and risk aversion 0.5.

Override the paths with `--model-dir`, `--forecasts`, `--features` and `--out-dir`. Select an issuance with `--forecast-at 2026-11-03T15:00:00Z`. Forecasts and models must match for local attribution.

## Outputs

The command writes these files under `results/step9/`.

- `recommendations.json` contains historical recommendation examples, computed `why` sentences, and signed price/carbon contributions for appliance and EV comparisons.
- `price_slot_contributions.csv` and `carbon_slot_contributions.csv` contain the per-feature contributions, model reference values, postprocessing adjustment and reconstructed median for each slot.
- `feature_importance.csv` contains training split gain and normalised gain shares for all six quantile models.
- `feature_importance.png` shows grouped importance for the price and carbon p50 models.

The historical washing-machine example estimates GBP 0.10 lower cost and 0.039 kg lower emissions than starting at the earliest available slot. Recent electricity prices contribute approximately GBP 0.06 of the model's lower predicted cost. These are forecast comparisons for a historical example, distinct from the realised receipts in Step 8.

## Python integration

```python
from src.explain import model_explanations, explain_recommendation

# feature_rows contains exactly one forecast issuance.
contributions = model_explanations(feature_rows, "results/step8/models")

# plan comes from plan_appliance() or plan_ev(), using this forecast table.
response = plan.to_dict()
response.update(explain_recommendation(plan, forecast, contributions))
```

Compute contributions once per issuance and reuse them across appliance requests. `why` is available even when only published prices and official carbon forecasts are available, through `plan.to_dict()`. Detailed model attribution is optional. Battery output includes its computed action and net comparison sentence, while local feature attribution is provided for appliance and EV windows.

The FastAPI endpoints currently serve mocks. The integration step can use this response when connecting them to the real optimiser. The existing mock explanation is separate from these generated results.

## Meaning and validation

Local contributions explain the difference between predicted median outcomes for the chosen schedule and running at the earliest available slot. Contributions are energy-weighted, including fractional and non-contiguous EV charging slots, then converted to GBP or kg CO2. Negative contributions lower the predicted outcome. All groups, including the model reference and any carbon-floor adjustment, are retained so they sum to the model difference.

Step 5 sorts independently trained quantiles. When quantiles cross, the reported median can come from the p10 or p90 model. The explanation selects contributions from the actual middle prediction for each slot and separately records carbon clipping. Runtime checks confirm that feature contributions reconstruct model predictions, the feature order matches the saved models, and the supplied forecast matches the plan totals.

Published-price comparisons are labelled `published_prices` and receive no model-based price attribution. A forecast median differing from the supplied models is labelled `different_forecast_median`. Global gain shows how much a feature was used in training splits, without assigning a direction or claiming a local explanation.

Model contributions describe associations learned by the model. They do not establish causal weather effects or explain the optimiser's risk penalty, deadline constraints, or all reasons a window wins. Calibration changes the interval endpoints and leaves the median unchanged. The existing historical availability assumptions continue to apply.

The shared model loader now normalises Windows CRLF line endings before loading model text. Git newline conversion otherwise invalidates the tree byte offsets stored in LightGBM text files. A regression test loads equivalent LF and CRLF model files and compares their predictions.
