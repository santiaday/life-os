"""Canonical biomarker vocabulary.

Every lab source spells the same analyte differently. Whoop Advanced Labs
calls it `alanine_aminotransferase`; the Quest report says `ALT`; a typed-in
note might say "SGPT". Without a canonical key, a single blood draw ingested
from two places produces two disconnected half-panels -- which is exactly the
state the warehouse was in for the 2026-05-30 draw.

Reference ranges here are adult male ranges and are used only to compute a
`status` when the source didn't supply one. A source's own reference range
always wins.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize(label: str) -> str:
    return _NON_ALNUM.sub("_", (label or "").strip().lower()).strip("_")


@dataclass(frozen=True)
class Biomarker:
    key: str
    name: str
    category: str
    unit: str | None = None
    ref_low: float | None = None
    ref_high: float | None = None
    higher_is_better: bool | None = None


def _b(key, name, category, unit=None, lo=None, hi=None, hib=None) -> Biomarker:
    return Biomarker(key, name, category, unit, lo, hi, hib)


BIOMARKERS: tuple[Biomarker, ...] = (
    # liver
    _b("alt", "ALT (SGPT)", "liver", "U/L", 9, 46, False),
    _b("ast", "AST (SGOT)", "liver", "U/L", 10, 40, False),
    _b("alp", "Alkaline Phosphatase", "liver", "U/L", 36, 130),
    _b("ggt", "GGT", "liver", "U/L", 3, 70, False),
    _b("bilirubin_total", "Bilirubin, Total", "liver", "mg/dL", 0.2, 1.2),
    _b("bilirubin_direct", "Bilirubin, Direct", "liver", "mg/dL", 0.0, 0.3),
    _b("albumin", "Albumin", "liver", "g/dL", 3.9, 5.0, True),
    _b("globulin", "Globulin", "liver", "g/dL", 1.9, 3.7),
    _b("protein_total", "Protein, Total", "liver", "g/dL", 6.0, 8.5),
    _b("albumin_globulin_ratio", "Albumin/Globulin Ratio", "liver", "ratio", 1.2, 2.2),

    # kidney / electrolytes
    _b("bun", "Urea Nitrogen (BUN)", "kidney", "mg/dL", 7, 25),
    _b("creatinine", "Creatinine", "kidney", "mg/dL", 0.6, 1.3),
    _b("bun_creatinine_ratio", "BUN/Creatinine Ratio", "kidney", "ratio", 9, 20),
    _b("egfr", "eGFR", "kidney", "mL/min/1.73m2", 60, None, True),
    _b("uric_acid", "Uric Acid", "kidney", "mg/dL", 3.8, 8.4, False),
    _b("sodium", "Sodium", "electrolyte", "mmol/L", 135, 146),
    _b("potassium", "Potassium", "electrolyte", "mmol/L", 3.5, 5.3),
    _b("chloride", "Chloride", "electrolyte", "mmol/L", 98, 110),
    _b("co2", "Carbon Dioxide", "electrolyte", "mmol/L", 20, 32),
    _b("calcium", "Calcium", "electrolyte", "mg/dL", 8.6, 10.3),
    _b("calcium_corrected", "Calcium, Corrected", "electrolyte", "mg/dL", 8.6, 10.3),
    _b("magnesium", "Magnesium", "electrolyte", "mg/dL", 1.6, 2.3),
    _b("phosphorus", "Phosphorus", "electrolyte", "mg/dL", 2.5, 4.5),
    _b("osmolality", "Osmolality", "electrolyte", "mOsm/kg", 275, 295),

    # metabolic
    _b("glucose", "Glucose, Fasting", "metabolic", "mg/dL", 65, 99, False),
    _b("hba1c", "Hemoglobin A1c", "metabolic", "%", 4.0, 5.6, False),
    _b("insulin_fasting", "Insulin, Fasting", "metabolic", "uIU/mL", 2.0, 8.0, False),
    _b("homa_ir", "HOMA-IR", "metabolic", "index", 0.0, 1.9, False),

    # lipids
    _b("cholesterol_total", "Cholesterol, Total", "lipid", "mg/dL", 100, 199, False),
    _b("ldl_c", "LDL Cholesterol", "lipid", "mg/dL", 0, 99, False),
    _b("hdl_c", "HDL Cholesterol", "lipid", "mg/dL", 40, None, True),
    _b("triglycerides", "Triglycerides", "lipid", "mg/dL", 0, 149, False),
    _b("non_hdl_c", "Non-HDL Cholesterol", "lipid", "mg/dL", 0, 129, False),
    _b("apob", "Apolipoprotein B", "lipid", "mg/dL", 0, 90, False),
    _b("lp_a", "Lipoprotein(a)", "lipid", "nmol/L", 0, 75, False),
    _b("cholesterol_hdl_ratio", "Total/HDL Ratio", "lipid", "ratio", 0, 5.0, False),

    # inflammation / autoimmune
    _b("crp_hs", "C-Reactive Protein (hs)", "inflammation", "mg/L", 0, 3.0, False),
    _b("esr", "Sedimentation Rate (ESR)", "inflammation", "mm/h", 0, 15, False),
    _b("rheumatoid_factor", "Rheumatoid Factor", "autoimmune", "IU/mL", 0, 14, False),
    _b("ana_screen", "ANA Screen (IFA)", "autoimmune", "qualitative"),
    _b("hla_b27", "HLA-B27 Antigen", "autoimmune", "qualitative"),

    # hematology
    _b("wbc", "White Blood Cells", "hematology", "K/uL", 3.8, 10.8),
    _b("rbc", "Red Blood Cells", "hematology", "M/uL", 4.2, 5.8),
    _b("hemoglobin", "Hemoglobin", "hematology", "g/dL", 13.2, 17.1),
    _b("hematocrit", "Hematocrit", "hematology", "%", 38.5, 50.0),
    _b("mcv", "MCV", "hematology", "fL", 80, 100),
    _b("mch", "MCH", "hematology", "pg", 27, 33),
    _b("mchc", "MCHC", "hematology", "g/dL", 32, 36),
    _b("rdw", "RDW", "hematology", "%", 11, 15, False),
    _b("platelets", "Platelets", "hematology", "K/uL", 140, 400),
    _b("mpv", "MPV", "hematology", "fL", 7.5, 12.5),
    _b("neutrophils_pct", "Neutrophils", "hematology", "%", 40, 75),
    _b("lymphocytes_pct", "Lymphocytes", "hematology", "%", 14, 46),
    _b("monocytes_pct", "Monocytes", "hematology", "%", 3, 12),
    _b("eosinophils_pct", "Eosinophils", "hematology", "%", 0, 6),
    _b("basophils_pct", "Basophils", "hematology", "%", 0, 2),
    _b("ferritin", "Ferritin", "hematology", "ng/mL", 30, 400),
    _b("iron", "Iron, Total", "hematology", "ug/dL", 50, 180),
    _b("tibc", "TIBC", "hematology", "ug/dL", 250, 425),
    _b("transferrin_saturation", "Transferrin Saturation", "hematology", "%", 15, 50),

    # thyroid
    _b("tsh", "TSH", "thyroid", "uIU/mL", 0.4, 4.5),
    _b("free_t4", "Free T4", "thyroid", "ng/dL", 0.8, 1.8),
    _b("free_t3", "Free T3", "thyroid", "pg/mL", 2.3, 4.2),
    _b("total_t3", "Total T3", "thyroid", "ng/dL", 76, 181),
    _b("reverse_t3", "Reverse T3", "thyroid", "ng/dL", 8, 25),
    _b("tpo_antibodies", "TPO Antibodies", "thyroid", "IU/mL", 0, 34, False),

    # hormones
    _b("testosterone_total", "Testosterone, Total", "hormone", "ng/dL", 300, 1000, True),
    _b("testosterone_free", "Testosterone, Free", "hormone", "pg/mL", 46, 224, True),
    _b("shbg", "SHBG", "hormone", "nmol/L", 10, 57),
    _b("estradiol", "Estradiol", "hormone", "pg/mL", 10, 42),
    _b("dhea_s", "DHEA-Sulfate", "hormone", "ug/dL", 138, 475),
    _b("cortisol", "Cortisol", "hormone", "ug/dL", 4, 22),
    _b("lh", "LH", "hormone", "mIU/mL", 1.5, 9.3),
    _b("fsh", "FSH", "hormone", "mIU/mL", 1.6, 8.0),
    _b("prolactin", "Prolactin", "hormone", "ng/mL", 2, 18),
    _b("igf_1", "IGF-1", "hormone", "ng/mL", 100, 300),
    _b("psa", "PSA, Total", "hormone", "ng/mL", 0, 4.0, False),

    # vitamins
    _b("vitamin_d_25oh", "Vitamin D, 25-OH", "vitamin", "ng/mL", 30, 100, True),
    _b("vitamin_b12", "Vitamin B12", "vitamin", "pg/mL", 200, 1100, True),
    _b("folate", "Folate", "vitamin", "ng/mL", 3.0, None, True),
    _b("homocysteine", "Homocysteine", "vitamin", "umol/L", 0, 10.4, False),

    # muscle
    _b("ck_total", "Creatine Kinase, Total", "muscle", "U/L", 44, 1083, False),
    _b("ldh", "LDH", "muscle", "U/L", 121, 224),

    # absolute differential counts -- Whoop Advanced Labs reports both the
    # percentage and the absolute count for each white-cell line.
    _b("neutrophils_abs", "Neutrophils, Absolute", "hematology", "cells/uL", 1500, 7800),
    _b("lymphocytes_abs", "Lymphocytes, Absolute", "hematology", "cells/uL", 850, 3900),
    _b("monocytes_abs", "Monocytes, Absolute", "hematology", "cells/uL", 200, 950),
    _b("eosinophils_abs", "Eosinophils, Absolute", "hematology", "cells/uL", 15, 500),
    _b("basophils_abs", "Basophils, Absolute", "hematology", "cells/uL", 0, 200),

    # derived indices
    _b("anion_gap", "Anion Gap", "electrolyte", "mEq/L", 5, 15),
    _b("estimated_average_glucose", "Estimated Average Glucose", "metabolic",
       "mg/dL", 0, 114, False),
    _b("atherogenic_index_plasma", "Atherogenic Index of Plasma", "lipid",
       "index", None, 0.11, False),
    _b("ldl_hdl_ratio", "LDL/HDL Ratio", "lipid", "ratio", 0, 3.0, False),
    _b("triglycerides_hdl_ratio", "Triglycerides/HDL Ratio", "lipid", "ratio",
       0, 2.0, False),
    _b("remnant_cholesterol", "Remnant Cholesterol", "lipid", "mg/dL", 0, 24, False),
    _b("fib_4_index", "FIB-4 Index", "liver", "index", 0, 1.3, False),
    _b("systemic_immune_inflammation_index", "Systemic Immune-Inflammation Index",
       "inflammation", "index", None, 500, False),
)

BY_KEY = {b.key: b for b in BIOMARKERS}

# vendor slug / label -> canonical key
_ALIASES: dict[str, tuple[str, ...]] = {
    "alt": ("alt", "sgpt", "alanine_aminotransferase", "alanine_aminotransferase_alt",
            "alt_sgpt"),
    "ast": ("ast", "sgot", "aspartate_aminotransferase", "aspartate_aminotransferase_ast",
            "ast_sgot"),
    "alp": ("alp", "alkaline_phosphatase", "alkaline_phosotase", "alkaline_phos"),
    "ggt": ("ggt", "gamma_glutamyl_transferase"),
    "bilirubin_total": ("bilirubin_total", "total_bilirubin", "bilirubin"),
    "bilirubin_direct": ("bilirubin_direct", "direct_bilirubin"),
    "albumin": ("albumin",),
    "globulin": ("globulin", "globulin_calculated", "globulin_total"),
    "protein_total": ("protein_total", "total_protein"),
    "albumin_globulin_ratio": ("albumin_globulin_ratio", "a_g_ratio",
                               "albumin_globulin_ratio_calculated"),
    "bun": ("bun", "blood_urea_nitrogen", "urea_nitrogen_bun", "urea_nitrogen"),
    "creatinine": ("creatinine", "creatinine_serum"),
    "bun_creatinine_ratio": ("bun_creatinine_ratio", "urea_nitrogen_creatinine_ratio"),
    "egfr": ("egfr", "estimated_glomerular_filtration_rate", "gfr_estimated",
             "egfr_non_african_american"),
    "uric_acid": ("uric_acid",),
    "sodium": ("sodium",),
    "potassium": ("potassium",),
    "chloride": ("chloride",),
    "co2": ("co2", "carbon_dioxide", "carbon_dioxide_co2", "bicarbonate"),
    "calcium": ("calcium",),
    "calcium_corrected": ("calcium_corrected", "corrected_calcium"),
    "magnesium": ("magnesium",),
    "phosphorus": ("phosphorus", "phosphate"),
    "osmolality": ("osmolality", "osmolality_calculated"),
    "glucose": ("glucose", "glucose_fasting", "glucose_serum", "fasting_glucose"),
    "hba1c": ("hba1c", "hemoglobin_a1c", "a1c", "hgba1c"),
    "insulin_fasting": ("insulin_fasting", "insulin", "fasting_insulin"),
    "homa_ir": ("homa_ir",),
    "cholesterol_total": ("cholesterol_total", "total_cholesterol", "cholesterol"),
    "ldl_c": ("ldl_c", "ldl", "ldl_cholesterol", "ldl_cholesterol_calc",
              "ldl_chol_calc_nih"),
    "hdl_c": ("hdl_c", "hdl", "hdl_cholesterol"),
    "triglycerides": ("triglycerides", "triglyceride"),
    "non_hdl_c": ("non_hdl_c", "non_hdl_cholesterol"),
    "apob": ("apob", "apolipoprotein_b"),
    "lp_a": ("lp_a", "lipoprotein_a"),
    "cholesterol_hdl_ratio": ("cholesterol_hdl_ratio", "chol_hdl_ratio",
                              "total_hdl_ratio"),
    "crp_hs": ("crp_hs", "c_reactive_protein_crp", "c_reactive_protein",
               "hs_crp", "crp", "high_sensitivity_c_reactive_protein"),
    "esr": ("esr", "erythrocyte_sedimentation_rate_esr_wes",
            "erythrocyte_sedimentation_rate", "sedimentation_rate"),
    "rheumatoid_factor": ("rheumatoid_factor", "rf"),
    "ana_screen": ("ana_screen", "ana_screen_ifa", "antinuclear_antibodies"),
    "hla_b27": ("hla_b27", "hla_b27_antigen"),
    "wbc": ("wbc", "white_blood_cell_count", "white_blood_cells", "leukocytes"),
    "rbc": ("rbc", "red_blood_cell_count", "red_blood_cells"),
    "hemoglobin": ("hemoglobin", "hgb"),
    "hematocrit": ("hematocrit", "hct"),
    "mcv": ("mcv", "mean_corpuscular_volume"),
    "mch": ("mch", "mean_corpuscular_hemoglobin"),
    "mchc": ("mchc",),
    "rdw": ("rdw", "red_cell_distribution_width"),
    "platelets": ("platelets", "platelet_count"),
    "mpv": ("mpv", "mean_platelet_volume"),
    "neutrophils_pct": ("neutrophils_pct", "neutrophils", "neutrophils_relative"),
    "lymphocytes_pct": ("lymphocytes_pct", "lymphocytes", "lymphocytes_relative"),
    "monocytes_pct": ("monocytes_pct", "monocytes", "monocytes_relative"),
    "eosinophils_pct": ("eosinophils_pct", "eosinophils", "eosinophils_relative"),
    "basophils_pct": ("basophils_pct", "basophils", "basophils_relative"),
    "ferritin": ("ferritin",),
    "iron": ("iron", "iron_total", "iron_serum"),
    "tibc": ("tibc", "total_iron_binding_capacity", "iron_binding_capacity"),
    "transferrin_saturation": ("transferrin_saturation", "iron_saturation",
                               "saturation"),
    "tsh": ("tsh", "thyroid_stimulating_hormone"),
    "free_t4": ("free_t4", "t4_free", "thyroxine_free", "ft4"),
    "free_t3": ("free_t3", "t3_free", "triiodothyronine_free", "ft3"),
    "total_t3": ("total_t3", "t3_total", "triiodothyronine_total"),
    "reverse_t3": ("reverse_t3", "rt3"),
    "tpo_antibodies": ("tpo_antibodies", "thyroid_peroxidase_antibodies"),
    "testosterone_total": ("testosterone_total", "testosterone",
                           "total_testosterone"),
    "testosterone_free": ("testosterone_free", "free_testosterone"),
    "shbg": ("shbg", "sex_hormone_binding_globulin"),
    "estradiol": ("estradiol", "e2"),
    "dhea_s": ("dhea_s", "dhea_sulfate", "dheas"),
    "cortisol": ("cortisol", "cortisol_am"),
    "lh": ("lh", "luteinizing_hormone"),
    "fsh": ("fsh", "follicle_stimulating_hormone"),
    "prolactin": ("prolactin",),
    "igf_1": ("igf_1", "igf1", "insulin_like_growth_factor_1"),
    "psa": ("psa", "psa_total", "prostate_specific_antigen"),
    "vitamin_d_25oh": ("vitamin_d_25oh", "vitamin_d", "vitamin_d_25_hydroxy",
                       "vitamin_d_25_oh_total", "25_hydroxyvitamin_d"),
    "vitamin_b12": ("vitamin_b12", "vitamin_b12_cobalamin", "b12", "cobalamin"),
    "folate": ("folate", "folic_acid", "folate_serum"),
    "homocysteine": ("homocysteine",),
    "ck_total": ("ck_total", "creatine_kinase_total", "creatine_kinase_ck_total",
                 "creatine_kinase", "cpk"),
    "ldh": ("ldh", "lactate_dehydrogenase"),
    "neutrophils_abs": ("neutrophils_abs", "absolute_neutrophils",
                        "neutrophils_absolute"),
    "lymphocytes_abs": ("lymphocytes_abs", "absolute_lymphocytes",
                        "lymphocytes_absolute"),
    "monocytes_abs": ("monocytes_abs", "absolute_monocytes", "monocytes_absolute"),
    "eosinophils_abs": ("eosinophils_abs", "absolute_eosinophils",
                        "eosinophils_absolute"),
    "basophils_abs": ("basophils_abs", "absolute_basophils", "basophils_absolute"),
    "anion_gap": ("anion_gap",),
    "estimated_average_glucose": ("estimated_average_glucose", "eag"),
    "atherogenic_index_plasma": ("atherogenic_index_plasma",
                                 "atherogenic_index_of_plasma", "aip"),
    "ldl_hdl_ratio": ("ldl_hdl_ratio",),
    "triglycerides_hdl_ratio": ("triglycerides_hdl_ratio", "trig_hdl_ratio"),
    "remnant_cholesterol": ("remnant_cholesterol",),
    "fib_4_index": ("fib_4_index", "fib4", "fib_4"),
    "systemic_immune_inflammation_index": ("systemic_immune_inflammation_index",
                                           "sii"),
}

# Percent-suffixed spellings of the differential, and a few vendor truncations.
_ALIASES["neutrophils_pct"] += ("neutrophils_percent",)
_ALIASES["lymphocytes_pct"] += ("lymphocytes_percent",)
_ALIASES["monocytes_pct"] += ("monocytes_percent",)
_ALIASES["eosinophils_pct"] += ("eosinophils_percent",)
_ALIASES["basophils_pct"] += ("basophils_percent",)
_ALIASES["rdw"] += ("red_cell_distribution_width_rdw",)
_ALIASES["dhea_s"] += ("dehydroepiandrosterone_sulfate",)
_ALIASES["homa_ir"] += ("homa_ir_score",)
_ALIASES["transferrin_saturation"] += ("iron_percent_saturation",)
_ALIASES["lp_a"] += ("lipoprotein",)
# Whoop truncates long slugs at 38 characters.
_ALIASES["esr"] += ("erythrocyte_sedimentation_rate_esr_wes",
                    "erythrocyte_sedimentation_rate_esr_westergren")

ALIAS_TO_KEY: dict[str, str] = {}
for _k, _vals in _ALIASES.items():
    ALIAS_TO_KEY[_k] = _k
    for _v in _vals:
        ALIAS_TO_KEY[_v] = _k


def resolve(vendor_key: str, display_name: str | None = None) -> tuple[str | None, bool]:
    """Vendor slug/label -> (canonical key, is_known). Returns (None, False)
    when nothing matches, so the caller can keep the vendor key and surface it
    for review rather than inventing a biomarker."""
    for cand in (normalize(vendor_key), normalize(display_name or "")):
        if not cand:
            continue
        hit = ALIAS_TO_KEY.get(cand)
        if hit:
            return hit, True
    return None, False


def status_for(key: str, value: float | None,
               ref_low: float | None = None,
               ref_high: float | None = None) -> str | None:
    """Classify a value. The source's own reference range wins; the catalogue
    range is only a fallback."""
    if value is None:
        return None
    b = BY_KEY.get(key)
    lo = ref_low if ref_low is not None else (b.ref_low if b else None)
    hi = ref_high if ref_high is not None else (b.ref_high if b else None)
    if lo is not None and value < lo:
        return "low"
    if hi is not None and value > hi:
        return "high"
    if lo is None and hi is None:
        return None
    return "normal"
