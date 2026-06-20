from engine.predi_brain import (
    BRAIN_DB_PATH,
    ensure_brain_db,
    sync_trade_db_samples,
    evaluate_pending_observations,
    backfill_history_experience,
    train_model_from_observations,
)
from engine.predi_torch_model import get_torch_status, train_torch_model_from_db

# Put your active tickers here if you want to force history backfill for them.
# Examples: ["SMLT", "ETLN", "IMOEXF", "CNYRUB_TOM"]
PREFERRED_TICKERS = []

print("BRAIN DB:", BRAIN_DB_PATH)

ensure_brain_db()

print("\nBefore:")
print(get_torch_status(BRAIN_DB_PATH))

print("\nSync trade journal samples:")
print(sync_trade_db_samples())

print("\nEvaluate pending live observations:")
print(evaluate_pending_observations())

print("\nBackfill historical observations:")
print(backfill_history_experience(force=True, preferred_tickers=PREFERRED_TICKERS))

print("\nTrain logistic brain first:")
print(train_model_from_observations())

print("\nTrain PyTorch:")
result = train_torch_model_from_db(BRAIN_DB_PATH, force=True)
print(result)

print("\nAfter:")
print(get_torch_status(BRAIN_DB_PATH))