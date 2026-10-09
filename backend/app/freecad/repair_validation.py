"""CAD implementation of the execution layer's repair-verification port."""
from app.freecad.failure_snapshot import validate_snapshot
from app.freecad.constraint_patch import verify_compiled_contract, verify_native_receipt
from app.freecad.profile_replan import verify_contract as verify_profile_contract, verify_receipt as verify_profile_receipt
from app.freecad.sketch_relations import verify_relation_receipts


class NativeConstraintRepairValidation:
    validate_snapshot = staticmethod(validate_snapshot)
    verify_contract = staticmethod(verify_compiled_contract)
    verify_receipt = staticmethod(verify_native_receipt)
    verify_relation_receipts = staticmethod(verify_relation_receipts)
    verify_profile_contract = staticmethod(verify_profile_contract)
    verify_profile_receipt = staticmethod(verify_profile_receipt)
