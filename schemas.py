from typing import Literal
from pydantic import BaseModel, Field

class Report(BaseModel):
    classification: Literal["application", "memory", "pcie", "interconnect", "thermal", "none"]
    severity: Literal["low", "medium", "high", "critical"]
    action: Literal["monitor", "gpu_reset", "drain_reboot", "rma", "investigate_workload", "inspect_cooling"]
    root_cause: str = Field(min_length=20)
    evidence: list[str] = Field(min_length=2, description="Concrete numbers/lines taken from your own run_python output")
    confidence: float = Field(ge=0, le=1)

XIDS = {
    13: ("Graphics Engine Exception", "Usually an application bug (illegal memory access, bad kernel). Rarely hardware. Check which pid and whether other jobs/GPUs are affected."),
    31: ("GPU memory page fault", "Typically application error; hardware only if it correlates with ECC errors."),
    48: ("Double Bit ECC Error", "Uncorrectable ECC. Check row-remap status; repeated DBEs indicate failing HBM."),
    63: ("ECC page retirement / row-remap recorded", "A row was remapped due to ECC error. A few are normal; many in a short time indicate failing memory."),
    64: ("ECC page retirement / row-remap FAILURE", "No spare rows left or remap failed. Memory is degraded: GPU should be drained and RMA'd."),
    74: ("NVLink error", "NVLink fatal/CRC errors. Isolated: reset the GPU. Persistent after reset: RMA / check baseboard."),
    79: ("GPU has fallen off the bus", "PCIe link lost. Usually needs drain + reboot; recurring after reboot means RMA (card/slot/riser)."),
    94: ("Contained ECC error", "Contained uncorrectable ECC error; affected process killed, GPU may continue. Check remap counters."),
    95: ("Uncontained ECC error", "Uncontained ECC: GPU reset required."),
}
