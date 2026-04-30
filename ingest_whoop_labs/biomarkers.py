"""Curated reference data for every biomarker Whoop Advanced Labs reports.

The Whoop JSON payload only carries each biomarker's *current value* and a
normalized 0–1 range meter (no absolute reference range bounds). To make these
results useful when answering health questions we attach:

  - description        : what the biomarker measures
  - category           : grouping (Cardiometabolic, Liver, Hormones, …)
  - unit               : preferred display unit
  - optimal_low / high : tight clinical target range (Whoop's "Optimal" band).
                         Values inside this band classify as OPTIMAL.
  - sufficient_low/high: outer acceptable range. Values outside this classify
                         as OUT_OF_RANGE in Whoop's UI.
  - what_high_means    : clinical interpretation when the value exceeds optimal
  - what_low_means     : clinical interpretation when the value drops below
  - influenced_by      : what nudges the biomarker (lifestyle, illness, drugs)

Ranges are tuned to a reproductively-aged adult male (the user). Anything
female-cycle-dependent or pediatric is out of scope here. Where Whoop's
"Optimal" target is meaningfully tighter than the lab "Reference" range, both
are reflected (sufficient_* matches the broader reference range).

Sources: clinical reference ranges that are widely published (Whoop's own
education tiles, Quest/LabCorp/Mayo reference, plus standard internal medicine
references). When in doubt the band is set conservatively wide — the MCP will
surface the value alongside Whoop's own status classification, which is the
ground truth for "is this in/out of range".
"""

from __future__ import annotations

# Each entry maps biomarker_id -> {title, category, unit, description, ...}
# biomarker_id matches Whoop's slug exactly (so we can join straight onto the
# JSON without a translation step).

BIOMARKERS: dict[str, dict] = {
    # ---- Cardiometabolic / Lipids -----------------------------------------
    "total_cholesterol": {
        "title": "Total Cholesterol",
        "category": "Cardiometabolic",
        "unit": "mg/dL",
        "description": (
            "Sum of all cholesterol carried by lipoproteins (LDL + HDL + ~20% "
            "of triglycerides). A blunt cardiovascular risk marker — the "
            "particle breakdown (ApoB, LDL, HDL, remnant) is far more "
            "predictive than the total."
        ),
        "optimal_low": 125, "optimal_high": 180,
        "sufficient_low": 125, "sufficient_high": 200,
        "what_high_means": (
            "Elevated cardiovascular risk if driven by ApoB-bearing particles "
            "(LDL, remnant). Always read together with ApoB and LDL — high "
            "total with high HDL can be benign."
        ),
        "what_low_means": (
            "Below ~125 mg/dL is uncommon; possible causes include "
            "malabsorption, hyperthyroidism, malnutrition, or aggressive "
            "statin therapy."
        ),
        "influenced_by": "Diet (saturated fat), genetics, statins, thyroid.",
    },
    "ldl_cholesterol": {
        "title": "LDL Cholesterol",
        "category": "Cardiometabolic",
        "unit": "mg/dL",
        "description": (
            "Low-density lipoprotein cholesterol — the cargo of the LDL "
            "particles that drive atherosclerotic plaque formation. Optimal is "
            "<100 mg/dL for general population; <70 mg/dL for those with "
            "established cardiovascular disease."
        ),
        "optimal_low": 0, "optimal_high": 100,
        "sufficient_low": 0, "sufficient_high": 130,
        "what_high_means": (
            "Higher = more atherogenic particles in circulation = greater "
            "long-term plaque burden. Pair with ApoB for the truer particle "
            "count."
        ),
        "what_low_means": (
            "Very low LDL (<40) is generally safe and seen with statin/PCSK9 "
            "therapy or rare familial hypobetalipoproteinemia."
        ),
        "influenced_by": "Saturated fat intake, genetics, statins, fiber, weight.",
    },
    "hdl_cholesterol": {
        "title": "HDL Cholesterol",
        "category": "Cardiometabolic",
        "unit": "mg/dL",
        "description": (
            "High-density lipoprotein cholesterol — reverse-transport "
            "particles that move cholesterol from peripheral tissues back to "
            "the liver. Higher HDL is loosely associated with lower CV risk, "
            "though function (efflux capacity) matters more than level."
        ),
        "optimal_low": 60, "optimal_high": 100,
        "sufficient_low": 40, "sufficient_high": 100,
        "what_high_means": (
            "Generally favorable, but extreme HDL (>100) can paradoxically "
            "associate with worse outcomes. Genetics-driven."
        ),
        "what_low_means": (
            "<40 mg/dL in men is a metabolic-syndrome flag. Often co-travels "
            "with high triglycerides, insulin resistance, low activity."
        ),
        "influenced_by": "Aerobic exercise, alcohol (mild ↑), niacin, genetics, weight loss.",
    },
    "non_hdl_cholesterol": {
        "title": "Non-HDL Cholesterol",
        "category": "Cardiometabolic",
        "unit": "mg/dL",
        "description": (
            "Total cholesterol minus HDL — captures all atherogenic particle "
            "cholesterol (LDL + VLDL + remnants + Lp(a)). Often a better risk "
            "marker than LDL alone, especially in metabolic syndrome / high-"
            "triglyceride states."
        ),
        "optimal_low": 0, "optimal_high": 130,
        "sufficient_low": 0, "sufficient_high": 160,
        "what_high_means": "Same direction as LDL — more atherogenic cholesterol cargo.",
        "what_low_means": "Generally favorable; no lower clinical concern.",
        "influenced_by": "Diet, genetics, lipid-lowering therapy.",
    },
    "remnant_cholesterol": {
        "title": "Remnant Cholesterol",
        "category": "Cardiometabolic",
        "unit": "mg/dL",
        "description": (
            "Cholesterol carried by triglyceride-rich remnant lipoproteins "
            "(VLDL remnants, IDL). Calculated as Total – LDL – HDL. Remnants "
            "are independently atherogenic — Mendelian-randomization studies "
            "show causal CV risk above and beyond LDL."
        ),
        "optimal_low": 0, "optimal_high": 20,
        "sufficient_low": 0, "sufficient_high": 30,
        "what_high_means": (
            "Marker of residual cardiovascular risk after LDL is controlled. "
            "Reflects insulin resistance / TG-rich lipoprotein excess."
        ),
        "what_low_means": "Favorable.",
        "influenced_by": "Carb intake, alcohol, insulin resistance, fasting state.",
    },
    "triglycerides": {
        "title": "Triglycerides",
        "category": "Cardiometabolic",
        "unit": "mg/dL",
        "description": (
            "Fasting triglycerides — the storage fat carried in VLDL and "
            "chylomicrons. Best assessed fasting (>10h). High TG is a strong "
            "indicator of insulin resistance and metabolic syndrome."
        ),
        "optimal_low": 0, "optimal_high": 100,
        "sufficient_low": 0, "sufficient_high": 150,
        "what_high_means": (
            "Insulin resistance, excess carb/alcohol intake, hypothyroidism, "
            "or familial hypertriglyceridemia. >500 raises pancreatitis risk."
        ),
        "what_low_means": "Generally favorable, especially when low-carb adapted.",
        "influenced_by": "Refined carbs, alcohol, fasting status, exercise, weight.",
    },
    "apolipoprotein_b": {
        "title": "Apolipoprotein B (ApoB)",
        "category": "Cardiometabolic",
        "unit": "mg/dL",
        "description": (
            "Single ApoB protein per atherogenic particle (LDL, VLDL, IDL, "
            "Lp(a)). Therefore ApoB ≈ total atherogenic particle count — the "
            "most direct measure of cardiovascular risk available on a "
            "standard panel. Lower is better, with no known floor."
        ),
        "optimal_low": 0, "optimal_high": 80,
        "sufficient_low": 0, "sufficient_high": 100,
        "what_high_means": (
            "More plaque-forming particles in circulation. Even with normal "
            "LDL, high ApoB indicates many small dense LDL particles → higher "
            "risk."
        ),
        "what_low_means": "Strongly cardio-protective. Aim for <80, ideally <60.",
        "influenced_by": "Saturated fat, statins, PCSK9 inhibitors, genetics, weight.",
    },
    "lipoprotein": {
        "title": "Lipoprotein (a)",
        "category": "Cardiometabolic",
        "unit": "nmol/L",
        "description": (
            "Lp(a) — an LDL particle bound to apolipoprotein(a). Largely "
            "genetic (set for life by ~age 5) and an independent CV risk "
            "factor. Test once; no reliable lifestyle modifier exists yet."
        ),
        "optimal_low": 0, "optimal_high": 75,
        "sufficient_low": 0, "sufficient_high": 125,
        "what_high_means": (
            ">125 nmol/L is a flag for elevated lifelong CV/aortic-valve risk. "
            "Mitigation focuses on aggressive control of OTHER risk factors "
            "(LDL, BP). PCSK9 inhibitors lower Lp(a) modestly."
        ),
        "what_low_means": "Favorable, lifelong.",
        "influenced_by": (
            "Genetics (LPA gene). Resistant to diet/exercise/statins. PCSK9 "
            "inhibitors and emerging RNAi (pelacarsen, olpasiran) lower it."
        ),
    },
    "cholesterol_hdl_ratio": {
        "title": "Cholesterol / HDL Ratio",
        "category": "Cardiometabolic",
        "unit": "(calc)",
        "description": (
            "Total cholesterol divided by HDL. A back-of-envelope CV risk "
            "ratio. ApoB or non-HDL is more useful, but the ratio is widely "
            "cited."
        ),
        "optimal_low": 0, "optimal_high": 3.5,
        "sufficient_low": 0, "sufficient_high": 5.0,
        "what_high_means": "Either total cholesterol is high or HDL is low — both unfavorable.",
        "what_low_means": "Favorable lipid balance.",
        "influenced_by": "Diet, exercise, genetics, weight.",
    },
    "ldl_hdl_ratio": {
        "title": "LDL / HDL Ratio",
        "category": "Cardiometabolic",
        "unit": "ratio",
        "description": (
            "LDL divided by HDL. Same idea as Total/HDL — a quick balance "
            "marker. <2 is generally protective."
        ),
        "optimal_low": 0, "optimal_high": 2.0,
        "sufficient_low": 0, "sufficient_high": 3.5,
        "what_high_means": "More atherogenic LDL relative to protective HDL.",
        "what_low_means": "Favorable.",
        "influenced_by": "Diet, exercise, genetics, weight.",
    },
    "triglycerides_hdl_ratio": {
        "title": "Triglycerides / HDL Ratio",
        "category": "Cardiometabolic",
        "unit": "ratio",
        "description": (
            "TG/HDL — proxy for insulin resistance and small-dense LDL "
            "particle burden. <2 is metabolically excellent; >3.5 raises "
            "concern."
        ),
        "optimal_low": 0, "optimal_high": 2.0,
        "sufficient_low": 0, "sufficient_high": 3.5,
        "what_high_means": "Insulin resistance, atherogenic dyslipidemia, metabolic syndrome.",
        "what_low_means": "Favorable metabolic state.",
        "influenced_by": "Carb intake, alcohol, insulin sensitivity, exercise.",
    },
    "atherogenic_index_of_plasma": {
        "title": "Atherogenic Index of Plasma (AIP)",
        "category": "Cardiometabolic",
        "unit": "index",
        "description": (
            "log10(TG/HDL). Single composite for atherogenic dyslipidemia "
            "burden. Independently predicts CV events."
        ),
        "optimal_low": -0.3, "optimal_high": 0.1,
        "sufficient_low": -0.5, "sufficient_high": 0.24,
        "what_high_means": ">0.24 = high CV risk profile (small dense LDL, high TG, low HDL).",
        "what_low_means": "Favorable.",
        "influenced_by": "Carb intake, exercise, weight, alcohol.",
    },

    # ---- Glucose / Insulin metabolism -------------------------------------
    "glucose": {
        "title": "Glucose",
        "category": "Cardiometabolic",
        "unit": "mg/dL",
        "description": (
            "Fasting blood glucose. Single-point readout of glucose homeostasis. "
            "<100 mg/dL = normoglycemia; 100-125 = pre-diabetes; ≥126 (twice) = "
            "diabetes. Whoop targets the tighter optimal range of 70-90."
        ),
        "optimal_low": 70, "optimal_high": 90,
        "sufficient_low": 70, "sufficient_high": 99,
        "what_high_means": "Insulin resistance, pre-diabetes, stress, recent meal.",
        "what_low_means": "Hypoglycemia risk if <70 with symptoms; rarely insulinoma.",
        "influenced_by": "Recent meals, sleep deprivation, stress, exercise, illness.",
    },
    "hemoglobin_a1c": {
        "title": "Hemoglobin A1c (HbA1c)",
        "category": "Cardiometabolic",
        "unit": "%",
        "description": (
            "Glycated hemoglobin — average glucose exposure over the prior "
            "~3 months. <5.7% normal; 5.7-6.4 pre-diabetic; ≥6.5 diabetic. "
            "Optimal is 4.5-5.4 by Whoop's tighter banding."
        ),
        "optimal_low": 4.5, "optimal_high": 5.4,
        "sufficient_low": 4.5, "sufficient_high": 5.6,
        "what_high_means": "Sustained higher glucose; insulin resistance / diabetes risk.",
        "what_low_means": (
            "<4.5% can indicate hemolytic anemia or shortened RBC life span "
            "(falsely lowers A1c). Otherwise favorable."
        ),
        "influenced_by": "Diet, exercise, sleep, weight, anemia state.",
    },
    "estimated_average_glucose": {
        "title": "Estimated Average Glucose (eAG)",
        "category": "Cardiometabolic",
        "unit": "mg/dL",
        "description": (
            "Direct mg/dL conversion of HbA1c. Same information; presented in "
            "the units a CGM reports. eAG = 28.7 × A1c − 46.7."
        ),
        "optimal_low": 80, "optimal_high": 100,
        "sufficient_low": 80, "sufficient_high": 120,
        "what_high_means": "Same as elevated A1c — sustained high glucose.",
        "what_low_means": "Same as low A1c.",
        "influenced_by": "Same as A1c.",
    },
    "insulin": {
        "title": "Insulin",
        "category": "Cardiometabolic",
        "unit": "uIU/mL",
        "description": (
            "Fasting insulin. Lower fasting insulin = better insulin "
            "sensitivity. <5 uIU/mL is exceptional metabolic health; >10 "
            "begins to suggest insulin resistance."
        ),
        "optimal_low": 2, "optimal_high": 5,
        "sufficient_low": 2, "sufficient_high": 10,
        "what_high_means": "Insulin resistance, hyperinsulinemia, pre-diabetes.",
        "what_low_means": "Favorable insulin sensitivity (also seen in fasted state, type 1 diabetes).",
        "influenced_by": "Fasting status, recent carbs, body composition, exercise, sleep.",
    },
    "homa_ir_score": {
        "title": "HOMA-IR Score",
        "category": "Cardiometabolic",
        "unit": "(calc)",
        "description": (
            "Homeostatic Model Assessment of Insulin Resistance: "
            "(fasting glucose × fasting insulin) / 405. Below 1 is excellent; "
            ">2.5 suggests insulin resistance."
        ),
        "optimal_low": 0, "optimal_high": 1.0,
        "sufficient_low": 0, "sufficient_high": 2.5,
        "what_high_means": "Insulin resistance, metabolic syndrome.",
        "what_low_means": "Excellent insulin sensitivity.",
        "influenced_by": "Diet quality, body fat, exercise, sleep.",
    },

    # ---- Liver -------------------------------------------------------------
    "alanine_aminotransferase": {
        "title": "Alanine Aminotransferase (ALT)",
        "category": "Liver",
        "unit": "U/L",
        "description": (
            "Liver enzyme, more liver-specific than AST. Released when "
            "hepatocytes are injured. Optimal is <25 U/L for men; classical "
            "lab cutoff is 40."
        ),
        "optimal_low": 0, "optimal_high": 25,
        "sufficient_low": 0, "sufficient_high": 40,
        "what_high_means": (
            "Hepatocyte injury — most commonly NAFLD, alcohol, viral hepatitis, "
            "drug effect (statins, acetaminophen), or recent intense exercise."
        ),
        "what_low_means": "Generally fine; very low can suggest B6 deficiency.",
        "influenced_by": "Alcohol, body fat, medications, viral hepatitis, intense exercise.",
    },
    "aspartate_aminotransferase": {
        "title": "Aspartate Aminotransferase (AST)",
        "category": "Liver",
        "unit": "U/L",
        "description": (
            "Liver enzyme also found in muscle and heart. AST/ALT ratio is "
            "informative — ratio >2 with elevated levels suggests alcoholic "
            "hepatitis; ratio <1 suggests NAFLD."
        ),
        "optimal_low": 0, "optimal_high": 25,
        "sufficient_low": 0, "sufficient_high": 40,
        "what_high_means": "Liver/muscle injury. Recent strenuous workout can raise AST.",
        "what_low_means": "No clinical concern.",
        "influenced_by": "Same as ALT plus muscle damage / rhabdo.",
    },
    "alkaline_phosotase": {
        "title": "Alkaline Phosphatase (ALP)",
        "category": "Liver",
        "unit": "U/L",
        "description": (
            "Enzyme from liver bile ducts and bone. Elevated when bile flow is "
            "obstructed (gallstones, cholestasis) or in high bone turnover "
            "(growth, fractures, Paget's)."
        ),
        "optimal_low": 40, "optimal_high": 90,
        "sufficient_low": 40, "sufficient_high": 130,
        "what_high_means": "Cholestasis, biliary obstruction, bone disease, pregnancy.",
        "what_low_means": "Zinc/magnesium deficiency, hypothyroidism, malnutrition.",
        "influenced_by": "Bile flow, bone turnover, zinc status.",
    },
    "total_bilirubin": {
        "title": "Total Bilirubin",
        "category": "Liver",
        "unit": "mg/dL",
        "description": (
            "Heme breakdown product cleared by the liver. Elevated when liver "
            "can't conjugate or excrete it (Gilbert's syndrome, hepatitis, bile "
            "obstruction, hemolysis)."
        ),
        "optimal_low": 0.2, "optimal_high": 1.0,
        "sufficient_low": 0.2, "sufficient_high": 1.2,
        "what_high_means": (
            "Mild ↑ often Gilbert's (benign, common in fasting/dehydration); "
            "marked ↑ = hepatitis, biliary obstruction, hemolysis."
        ),
        "what_low_means": "No clinical concern.",
        "influenced_by": "Fasting, hydration, Gilbert's, hemolysis, alcohol.",
    },
    "albumin": {
        "title": "Albumin",
        "category": "Liver",
        "unit": "g/dL",
        "description": (
            "Most abundant blood protein, made in the liver. Reflects "
            "hepatic synthetic function and nutritional status. Long half-life "
            "(~20 days) so changes lag."
        ),
        "optimal_low": 4.2, "optimal_high": 5.0,
        "sufficient_low": 3.5, "sufficient_high": 5.0,
        "what_high_means": "Almost always dehydration (concentration effect).",
        "what_low_means": "Liver disease, malnutrition, kidney loss (nephrotic), inflammation, burns.",
        "influenced_by": "Hydration, protein intake, illness, liver function.",
    },
    "globulin_calculated": {
        "title": "Globulin",
        "category": "Liver",
        "unit": "g/dL (calc)",
        "description": (
            "Total protein – albumin. Reflects immunoglobulins + inflammatory "
            "proteins. Elevated in chronic inflammation, infections, "
            "myeloma, autoimmune disease."
        ),
        "optimal_low": 2.0, "optimal_high": 3.0,
        "sufficient_low": 2.0, "sufficient_high": 3.5,
        "what_high_means": "Chronic inflammation, infection, autoimmunity, myeloma.",
        "what_low_means": "Immune deficiency, severe liver disease, malnutrition.",
        "influenced_by": "Infection, inflammation, immune status.",
    },
    "albumin_globulin_ratio": {
        "title": "Albumin/Globulin Ratio",
        "category": "Liver",
        "unit": "(calc)",
        "description": (
            "Albumin divided by globulin. <1 raises concern for "
            "myeloma/inflammation; >2 is favorable."
        ),
        "optimal_low": 1.5, "optimal_high": 2.5,
        "sufficient_low": 1.1, "sufficient_high": 2.5,
        "what_high_means": "Generally favorable.",
        "what_low_means": "Suggestive of inflammation, autoimmunity, or paraproteinemia.",
        "influenced_by": "Inflammation, immune state.",
    },
    "total_protein": {
        "title": "Total Protein",
        "category": "Liver",
        "unit": "g/dL",
        "description": (
            "Sum of albumin and globulin. Reflects synthetic capacity, "
            "hydration, immune state. Mostly informative through its components."
        ),
        "optimal_low": 6.5, "optimal_high": 8.0,
        "sufficient_low": 6.0, "sufficient_high": 8.3,
        "what_high_means": "Dehydration, chronic inflammation/infection, myeloma.",
        "what_low_means": "Malnutrition, liver disease, malabsorption.",
        "influenced_by": "Hydration, protein intake, liver, immune state.",
    },
    "fib_4_index": {
        "title": "FIB-4 Index",
        "category": "Liver",
        "unit": "index",
        "description": (
            "Composite liver-fibrosis score from age, AST, ALT, platelets. "
            "<1.3 effectively rules out advanced fibrosis; >2.67 raises concern. "
            "Best non-invasive screen for NAFLD progression."
        ),
        "optimal_low": 0, "optimal_high": 1.3,
        "sufficient_low": 0, "sufficient_high": 2.67,
        "what_high_means": ">1.3 → consider further fibrosis evaluation (Fibroscan / ELF).",
        "what_low_means": "No advanced fibrosis.",
        "influenced_by": "Age, alcohol, body fat, viral hepatitis.",
    },

    # ---- Kidney / Electrolytes --------------------------------------------
    "blood_urea_nitrogen": {
        "title": "Blood Urea Nitrogen",
        "category": "Kidney",
        "unit": "mg/dL",
        "description": (
            "Nitrogen waste from protein breakdown, cleared by the kidneys. "
            "Sensitive to hydration and dietary protein, less specific to "
            "kidney function than creatinine."
        ),
        "optimal_low": 9, "optimal_high": 18,
        "sufficient_low": 7, "sufficient_high": 20,
        "what_high_means": "Dehydration, high-protein diet, GI bleed, kidney dysfunction.",
        "what_low_means": "Low protein intake, overhydration, severe liver disease.",
        "influenced_by": "Hydration, protein intake, GI bleeding, kidney function.",
    },
    "creatinine": {
        "title": "Creatinine",
        "category": "Kidney",
        "unit": "mg/dL",
        "description": (
            "Muscle metabolism waste cleared exclusively by the kidneys. "
            "Primary input to eGFR. Higher muscle mass raises baseline "
            "creatinine — a fit person at 1.1 isn't 'kidney impaired'."
        ),
        "optimal_low": 0.7, "optimal_high": 1.1,
        "sufficient_low": 0.6, "sufficient_high": 1.3,
        "what_high_means": "Reduced kidney filtration, dehydration, high muscle mass, recent intense exercise, creatine supplementation.",
        "what_low_means": "Low muscle mass, severe liver disease, pregnancy.",
        "influenced_by": "Muscle mass, hydration, creatine supplementation, kidney function.",
    },
    "estimated_glomerular_filtration_rate": {
        "title": "Estimated Glomerular Filtration Rate (eGFR)",
        "category": "Kidney",
        "unit": "mL/min/1.73m2",
        "description": (
            "Estimated kidney filtration rate from creatinine, age, sex. "
            ">90 normal; 60-89 mildly reduced; <60 indicates CKD if persistent. "
            "Whoop's 'OUT_OF_RANGE' for hyper-filtrated values (>120) flags "
            "early diabetic kidney hyperfiltration in some contexts but in "
            "young, lean, well-hydrated males it's almost always benign."
        ),
        "optimal_low": 90, "optimal_high": 120,
        "sufficient_low": 60, "sufficient_high": 120,
        "what_high_means": (
            "Hyperfiltration — usually benign in young/lean people, but in "
            "diabetics it can precede nephropathy."
        ),
        "what_low_means": "Reduced kidney function. <60 sustained = CKD.",
        "influenced_by": "Hydration, age, muscle mass, BP, diabetes.",
    },
    "bun_creatinine_ratio": {
        "title": "BUN/Creatinine Ratio",
        "category": "Kidney",
        "unit": "",
        "description": (
            "Helps distinguish pre-renal causes (dehydration, GI bleed → ratio "
            ">20) from intrinsic renal disease (ratio 10-20)."
        ),
        "optimal_low": 10, "optimal_high": 16,
        "sufficient_low": 10, "sufficient_high": 20,
        "what_high_means": "Dehydration, GI bleed, high-protein diet, low muscle.",
        "what_low_means": "Low protein intake, liver disease, overhydration.",
        "influenced_by": "Hydration, protein intake, muscle mass.",
    },
    "anion_gap": {
        "title": "Anion Gap",
        "category": "Kidney",
        "unit": "mEq/L",
        "description": (
            "Sodium − (Chloride + Bicarbonate). Tracks unmeasured anions. "
            "Elevated in diabetic ketoacidosis, lactic acidosis, kidney failure, "
            "toxic ingestions. Low gap is rare and usually a lab artifact, "
            "though paraproteinemia, hypoalbuminemia, lithium, or bromism can "
            "cause it. A mildly low value with no symptoms is rarely clinically "
            "significant."
        ),
        "optimal_low": 7, "optimal_high": 13,
        "sufficient_low": 6, "sufficient_high": 14,
        "what_high_means": "Acidosis (DKA, lactic acidosis, uremia, toxins).",
        "what_low_means": "Usually lab variation; rarely paraproteinemia, hypoalbuminemia.",
        "influenced_by": "Acid-base status, albumin, lab calibration.",
    },
    "sodium": {
        "title": "Sodium",
        "category": "Kidney",
        "unit": "mmol/L",
        "description": (
            "Major extracellular cation. Driven by free-water balance more "
            "than salt intake. Tightly regulated."
        ),
        "optimal_low": 138, "optimal_high": 142,
        "sufficient_low": 135, "sufficient_high": 145,
        "what_high_means": "Dehydration / free-water loss.",
        "what_low_means": "Hyponatremia: SIADH, water overload, diuretics, beer-potomania, endurance overhydration.",
        "influenced_by": "Hydration, hormones (ADH, aldosterone), diuretics.",
    },
    "potassium": {
        "title": "Potassium",
        "category": "Kidney",
        "unit": "mmol/L",
        "description": (
            "Major intracellular cation; tight regulation matters because "
            "small swings affect cardiac excitability."
        ),
        "optimal_low": 4.0, "optimal_high": 4.8,
        "sufficient_low": 3.5, "sufficient_high": 5.1,
        "what_high_means": "Renal failure, ACEi/ARB/spironolactone, hemolysis (often artifact).",
        "what_low_means": "Diuretics, GI losses, refeeding, low intake.",
        "influenced_by": "Diet, kidney function, medications, sample handling.",
    },
    "chloride": {
        "title": "Chloride",
        "category": "Kidney",
        "unit": "mmol/L",
        "description": (
            "Tracks with sodium and reflects hydration / acid-base. Less "
            "useful in isolation."
        ),
        "optimal_low": 100, "optimal_high": 106,
        "sufficient_low": 98, "sufficient_high": 107,
        "what_high_means": "Dehydration, metabolic acidosis.",
        "what_low_means": "Vomiting, metabolic alkalosis, diuretics.",
        "influenced_by": "Hydration, acid-base.",
    },
    "carbon_dioxide": {
        "title": "Carbon Dioxide",
        "category": "Kidney",
        "unit": "mmol/L",
        "description": (
            "Total CO2 — a proxy for serum bicarbonate. Reflects acid-base "
            "balance. Low = metabolic acidosis; high = metabolic alkalosis or "
            "compensatory for respiratory acidosis."
        ),
        "optimal_low": 23, "optimal_high": 28,
        "sufficient_low": 22, "sufficient_high": 30,
        "what_high_means": "Metabolic alkalosis (vomiting, diuretics) or chronic CO2 retention.",
        "what_low_means": "Metabolic acidosis (DKA, lactic, renal).",
        "influenced_by": "Respiratory rate, vomiting, diuretics, kidney function.",
    },
    "calcium": {
        "title": "Calcium",
        "category": "Vitamins & Minerals",
        "unit": "mg/dL",
        "description": (
            "Total serum calcium (free + albumin-bound). Tightly regulated. "
            "Always interpret with albumin — corrected calcium is the "
            "albumin-adjusted reading."
        ),
        "optimal_low": 9.2, "optimal_high": 10.0,
        "sufficient_low": 8.5, "sufficient_high": 10.4,
        "what_high_means": "Hyperparathyroidism, malignancy, vitamin D toxicity.",
        "what_low_means": "Vitamin D deficiency, hypoparathyroidism, hypoalbuminemia.",
        "influenced_by": "Vitamin D, parathyroid, albumin, kidney function.",
    },
    "corrected_calcium": {
        "title": "Corrected Calcium",
        "category": "Vitamins & Minerals",
        "unit": "mg/dL",
        "description": (
            "Calcium adjusted for albumin (formula: Ca + 0.8 × (4 − albumin)). "
            "More accurate when albumin is abnormal."
        ),
        "optimal_low": 9.2, "optimal_high": 10.0,
        "sufficient_low": 8.5, "sufficient_high": 10.4,
        "what_high_means": "Same as calcium high.",
        "what_low_means": "Same as calcium low.",
        "influenced_by": "Same as calcium.",
    },
    "osmolality": {
        "title": "Plasma Osmolality",
        "category": "Kidney",
        "unit": "mOsm/kg",
        "description": (
            "Solute concentration in plasma. Driven mainly by sodium, glucose, "
            "BUN. Indicator of hydration status."
        ),
        "optimal_low": 280, "optimal_high": 295,
        "sufficient_low": 275, "sufficient_high": 300,
        "what_high_means": "Dehydration, hyperglycemia, uremia.",
        "what_low_means": "Overhydration, SIADH, low protein.",
        "influenced_by": "Hydration, sodium, glucose, ADH.",
    },

    # ---- Inflammation -----------------------------------------------------
    "high_sensitivity_c_reactive_protein": {
        "title": "High-Sensitivity C-Reactive Protein (hs-CRP)",
        "category": "Inflammation",
        "unit": "mg/L",
        "description": (
            "Acute-phase protein from the liver. hs-CRP <1 = low CV risk; "
            "1-3 = average; >3 = high. Often elevated with recent infection, "
            "intense training, or NAFLD."
        ),
        "optimal_low": 0, "optimal_high": 1.0,
        "sufficient_low": 0, "sufficient_high": 3.0,
        "what_high_means": (
            "Systemic inflammation. >3 mg/L sustained suggests metabolic "
            "syndrome / atherosclerotic risk; >10 suggests acute infection or "
            "flare."
        ),
        "what_low_means": "Favorable.",
        "influenced_by": (
            "Recent illness, intense exercise (can transiently raise), NSAIDs, "
            "statins lower it, body fat, sleep loss."
        ),
    },
    "systemic_immune_inflammation_index": {
        "title": "Systemic Immune-Inflammation Index (SII)",
        "category": "Inflammation",
        "unit": "index",
        "description": (
            "Neutrophils × Platelets / Lymphocytes. Composite immune state "
            "marker; higher values associate with worse outcomes in cardiac, "
            "oncologic, and metabolic studies."
        ),
        "optimal_low": 0, "optimal_high": 600,
        "sufficient_low": 0, "sufficient_high": 800,
        "what_high_means": "Pro-inflammatory state, possibly infection or chronic stress.",
        "what_low_means": "Generally favorable.",
        "influenced_by": "Infection, training load, sleep, chronic disease.",
    },

    # ---- CBC: Red blood cell line -----------------------------------------
    "red_blood_cell_count": {
        "title": "Red Blood Cell Count (RBC)",
        "category": "Blood Count",
        "unit": "Million/uL",
        "description": (
            "Number of red cells per microliter. Drops in anemia, rises in "
            "polycythemia / chronic hypoxia."
        ),
        "optimal_low": 4.5, "optimal_high": 5.5,
        "sufficient_low": 4.2, "sufficient_high": 5.9,
        "what_high_means": "Polycythemia, dehydration, smoking, altitude exposure.",
        "what_low_means": "Anemia (iron, B12, folate, blood loss, marrow problems).",
        "influenced_by": "Iron, B12, folate, kidney (EPO), altitude, hydration.",
    },
    "hemoglobin": {
        "title": "Hemoglobin",
        "category": "Blood Count",
        "unit": "g/dL",
        "description": (
            "Oxygen-carrying protein in red cells. Whoop targets a tighter "
            "optimal range than the standard reference (men 13.5-17.5)."
        ),
        "optimal_low": 14.5, "optimal_high": 16.5,
        "sufficient_low": 13.5, "sufficient_high": 17.5,
        "what_high_means": "Polycythemia, dehydration, altitude, smoking.",
        "what_low_means": "Anemia → impacts O2 delivery, recovery, performance.",
        "influenced_by": "Iron, B12, folate, training stimulus, altitude, hydration.",
    },
    "hematocrit": {
        "title": "Hematocrit",
        "category": "Blood Count",
        "unit": "%",
        "description": (
            "Percentage of blood volume that's red cells. Tracks with hemoglobin. "
            "Affected strongly by hydration."
        ),
        "optimal_low": 42, "optimal_high": 48,
        "sufficient_low": 40, "sufficient_high": 52,
        "what_high_means": "Polycythemia, dehydration, smoking, altitude.",
        "what_low_means": "Anemia.",
        "influenced_by": "Same as hemoglobin.",
    },
    "mcv": {
        "title": "Mean Corpuscular Volume (MCV)",
        "category": "Blood Count",
        "unit": "fL",
        "description": (
            "Average red cell size. <80 = microcytic (iron deficiency, "
            "thalassemia); >100 = macrocytic (B12/folate, alcohol, hypothyroid)."
        ),
        "optimal_low": 86, "optimal_high": 95,
        "sufficient_low": 80, "sufficient_high": 100,
        "what_high_means": "Macrocytosis: B12 deficiency, folate, alcohol, hypothyroidism.",
        "what_low_means": "Microcytosis: iron deficiency, thalassemia.",
        "influenced_by": "Iron, B12, folate, alcohol, thyroid.",
    },
    "mch": {
        "title": "Mean Corpuscular Hemoglobin (MCH)",
        "category": "Blood Count",
        "unit": "pg",
        "description": (
            "Average mass of hemoglobin per red cell. Tracks with MCV; "
            "informative alongside MCV/MCHC for anemia classification."
        ),
        "optimal_low": 28, "optimal_high": 32,
        "sufficient_low": 27, "sufficient_high": 33,
        "what_high_means": "Macrocytic anemia.",
        "what_low_means": "Microcytic / hypochromic anemia (iron, thalassemia).",
        "influenced_by": "Same as MCV.",
    },
    "mchc": {
        "title": "Mean Corpuscular Hemoglobin Concentration (MCHC)",
        "category": "Blood Count",
        "unit": "g/dL",
        "description": (
            "Hemoglobin density inside red cells. Distinguishes hypochromic "
            "vs normo/hyperchromic anemia."
        ),
        "optimal_low": 33, "optimal_high": 35,
        "sufficient_low": 32, "sufficient_high": 36,
        "what_high_means": "Hereditary spherocytosis, lab artifact.",
        "what_low_means": "Iron deficiency, thalassemia.",
        "influenced_by": "Iron status, RBC membrane integrity.",
    },
    "red_cell_distribution_width_rdw": {
        "title": "Red Cell Distribution Width (RDW)",
        "category": "Blood Count",
        "unit": "%",
        "description": (
            "Variability of red cell sizes. Elevated RDW with normal MCV is "
            "an early flag of iron deficiency, B12 deficiency, or mixed "
            "hematinic issues. Independently associated with all-cause mortality."
        ),
        "optimal_low": 11.5, "optimal_high": 13.5,
        "sufficient_low": 11.0, "sufficient_high": 14.5,
        "what_high_means": "Mixed populations of red cells: nutritional deficiency, hemolysis, recent transfusion.",
        "what_low_means": "Uniform red cell size — not clinically meaningful.",
        "influenced_by": "Iron, B12, folate, blood loss, transfusion.",
    },

    # ---- CBC: White blood cell line ---------------------------------------
    "white_blood_cell_count": {
        "title": "White Blood Cells (WBC)",
        "category": "Blood Count",
        "unit": "Thousand/uL",
        "description": (
            "Total leukocytes. Rises with infection, stress, exercise, "
            "steroids. Falls with viral infections, marrow suppression, severe "
            "sepsis."
        ),
        "optimal_low": 4.5, "optimal_high": 7.5,
        "sufficient_low": 4.0, "sufficient_high": 11.0,
        "what_high_means": "Infection, inflammation, stress, post-exercise, leukemia.",
        "what_low_means": "Viral infection, marrow suppression, autoimmune neutropenia.",
        "influenced_by": "Recent illness, exercise, training load, medications.",
    },
    "neutrophils_percent": {
        "title": "Neutrophil %",
        "category": "Blood Count",
        "unit": "%",
        "description": "Percentage of WBCs that are neutrophils. Rises in bacterial infection and stress.",
        "optimal_low": 40, "optimal_high": 60,
        "sufficient_low": 40, "sufficient_high": 70,
        "what_high_means": "Bacterial infection, acute inflammation, steroids, stress.",
        "what_low_means": "Viral infection, autoimmune, drug-induced neutropenia.",
        "influenced_by": "Infection, stress, medications.",
    },
    "absolute_neutrophils": {
        "title": "Neutrophils",
        "category": "Blood Count",
        "unit": "cells/uL",
        "description": "Absolute neutrophil count (ANC). <1500 = neutropenia (infection risk).",
        "optimal_low": 2000, "optimal_high": 6000,
        "sufficient_low": 1500, "sufficient_high": 7500,
        "what_high_means": "Bacterial infection, inflammation, stress.",
        "what_low_means": "Severe viral, autoimmune, drug-induced; <500 = serious infection risk.",
        "influenced_by": "Same as neutrophils %.",
    },
    "lymphocytes_percent": {
        "title": "Lymphocyte %",
        "category": "Blood Count",
        "unit": "%",
        "description": "Lymphocytes as a fraction of WBCs. Elevated with viral infection.",
        "optimal_low": 25, "optimal_high": 40,
        "sufficient_low": 20, "sufficient_high": 45,
        "what_high_means": "Viral infection, mono, pertussis, lymphocytic leukemia.",
        "what_low_means": "Steroids, severe stress, acute illness, HIV.",
        "influenced_by": "Viral exposure, stress hormones, training load.",
    },
    "absolute_lymphocytes": {
        "title": "Lymphocytes",
        "category": "Blood Count",
        "unit": "cells/uL",
        "description": "Absolute lymphocyte count.",
        "optimal_low": 1500, "optimal_high": 3500,
        "sufficient_low": 1000, "sufficient_high": 4800,
        "what_high_means": "Viral / chronic immune activation.",
        "what_low_means": "Stress, steroids, viral nadir, immunodeficiency.",
        "influenced_by": "Viral exposure, training, sleep, stress.",
    },
    "monocytes_percent": {
        "title": "Monocyte %",
        "category": "Blood Count",
        "unit": "%",
        "description": "Monocytes as a fraction of WBCs. Elevated with chronic infections, inflammation, stress.",
        "optimal_low": 4, "optimal_high": 8,
        "sufficient_low": 2, "sufficient_high": 9,
        "what_high_means": "Chronic infection, inflammation (TB, IBD), recovery from acute infection, autoimmune.",
        "what_low_means": "Bone marrow suppression, severe acute infection, steroid use.",
        "influenced_by": "Chronic infection, inflammation, training load, bone marrow state.",
    },
    "absolute_monocytes": {
        "title": "Monocytes",
        "category": "Blood Count",
        "unit": "cells/uL",
        "description": "Absolute monocyte count.",
        "optimal_low": 200, "optimal_high": 700,
        "sufficient_low": 100, "sufficient_high": 900,
        "what_high_means": "Chronic infection, inflammation.",
        "what_low_means": "Marrow suppression.",
        "influenced_by": "Same as monocytes %.",
    },
    "eosinophils_percent": {
        "title": "Eosinophil %",
        "category": "Blood Count",
        "unit": "%",
        "description": "Eosinophils — elevated in allergy, parasites, asthma, drug reaction.",
        "optimal_low": 0, "optimal_high": 4,
        "sufficient_low": 0, "sufficient_high": 6,
        "what_high_means": "Allergy, asthma, parasites, drug reaction, eosinophilic disorders.",
        "what_low_means": "Acute stress / steroid effect; not clinically concerning.",
        "influenced_by": "Allergens, asthma, parasites, medications, cortisol.",
    },
    "absolute_eosinophils": {
        "title": "Eosinophils",
        "category": "Blood Count",
        "unit": "cells/uL",
        "description": "Absolute eosinophil count.",
        "optimal_low": 0, "optimal_high": 400,
        "sufficient_low": 0, "sufficient_high": 600,
        "what_high_means": "Same as eosinophils %.",
        "what_low_means": "Same as eosinophils %.",
        "influenced_by": "Same as eosinophils %.",
    },
    "basophils_percent": {
        "title": "Basophil %",
        "category": "Blood Count",
        "unit": "%",
        "description": "Basophils — small fraction of WBCs. Rare clinical relevance unless markedly elevated.",
        "optimal_low": 0, "optimal_high": 1,
        "sufficient_low": 0, "sufficient_high": 2,
        "what_high_means": "Allergic reaction, hypothyroidism, chronic myeloid leukemia.",
        "what_low_means": "Acute stress, hyperthyroidism — usually not significant.",
        "influenced_by": "Allergens, thyroid, marrow disorders.",
    },
    "absolute_basophils": {
        "title": "Basophils",
        "category": "Blood Count",
        "unit": "cells/uL",
        "description": "Absolute basophil count.",
        "optimal_low": 0, "optimal_high": 100,
        "sufficient_low": 0, "sufficient_high": 200,
        "what_high_means": "Same as basophils %.",
        "what_low_means": "Same as basophils %.",
        "influenced_by": "Same as basophils %.",
    },
    "platelet_count": {
        "title": "Platelets",
        "category": "Blood Count",
        "unit": "Thousand/uL",
        "description": (
            "Cells that drive clotting. Rises with inflammation/iron deficiency; "
            "falls with marrow suppression, sepsis, autoimmune destruction."
        ),
        "optimal_low": 200, "optimal_high": 350,
        "sufficient_low": 150, "sufficient_high": 400,
        "what_high_means": "Reactive thrombocytosis (inflammation, iron deficiency), essential thrombocythemia.",
        "what_low_means": "ITP, viral suppression, alcohol, sepsis.",
        "influenced_by": "Inflammation, iron status, alcohol, marrow.",
    },
    "mpv": {
        "title": "Mean Platelet Volume (MPV)",
        "category": "Blood Count",
        "unit": "fL",
        "description": (
            "Average platelet size. Larger platelets are younger and more "
            "active; rises when marrow is producing platelets quickly."
        ),
        "optimal_low": 7.5, "optimal_high": 10.5,
        "sufficient_low": 7.0, "sufficient_high": 11.5,
        "what_high_means": "Active platelet production (post-bleed, ITP, inflammation).",
        "what_low_means": "Marrow suppression.",
        "influenced_by": "Marrow turnover, inflammation, recent blood loss.",
    },

    # ---- Hormones (sex / pituitary) ---------------------------------------
    "testosterone": {
        "title": "Testosterone",
        "category": "Hormones",
        "unit": "ng/dL",
        "description": (
            "Total serum testosterone (free + bound). Standard adult-male "
            "reference range is ~264-916 ng/dL; functional/optimal target is "
            "often 600-900. Whoop classifies <500 as 'sufficient' rather than "
            "'optimal' for this reason."
        ),
        "optimal_low": 600, "optimal_high": 900,
        "sufficient_low": 264, "sufficient_high": 916,
        "what_high_means": "Exogenous T use, anabolic steroids, rare testicular tumor.",
        "what_low_means": (
            "Hypogonadism — symptoms include low libido, fatigue, mood changes, "
            "reduced muscle mass. Causes: primary (testicular), secondary "
            "(pituitary), age, chronic stress, sleep loss, obesity."
        ),
        "influenced_by": (
            "Sleep, body fat, training, alcohol, stress, age, opioids, "
            "AAS history. Diurnal — draw before 10am for accuracy."
        ),
    },
    "free_testosterone": {
        "title": "Free Testosterone",
        "category": "Hormones",
        "unit": "pg/mL",
        "description": (
            "The biologically active fraction not bound to SHBG or albumin. "
            "More clinically meaningful than total T when SHBG is unusual. "
            "Adult-male reference ranges vary by lab and method (e.g. 35-155 "
            "pg/mL is common); Whoop's 'optimal' band runs higher."
        ),
        "optimal_low": 100, "optimal_high": 200,
        "sufficient_low": 35, "sufficient_high": 155,
        "what_high_means": "Same as total T elevation.",
        "what_low_means": "Functional hypogonadism even when total T appears normal — symptomatic low T.",
        "influenced_by": "SHBG level, total T, body fat, age, sleep.",
    },
    "sex_hormone_binding_globulin": {
        "title": "Sex Hormone Binding Globulin (SHBG)",
        "category": "Hormones",
        "unit": "nmol/L",
        "description": (
            "Liver-made protein that binds testosterone and estradiol. Low "
            "SHBG raises free hormone fractions; high SHBG suppresses them. "
            "Itself an insulin-resistance marker — low SHBG often co-travels "
            "with metabolic syndrome and NAFLD."
        ),
        "optimal_low": 20, "optimal_high": 45,
        "sufficient_low": 14, "sufficient_high": 60,
        "what_high_means": "Hyperthyroidism, liver disease, oral estrogens, aging.",
        "what_low_means": "Insulin resistance, obesity, hypothyroidism, anabolic steroid use.",
        "influenced_by": "Liver state, insulin resistance, thyroid, exogenous hormones.",
    },
    "luteinizing_hormone": {
        "title": "Luteinizing Hormone (LH)",
        "category": "Hormones",
        "unit": "mIU/mL",
        "description": (
            "Pituitary gonadotropin that stimulates testicular Leydig cells "
            "to make testosterone. Adult-male reference ~1.7-8.6 mIU/mL. "
            "Whoop flags lower values — suppressed LH with low T points to "
            "secondary hypogonadism (pituitary/hypothalamic) rather than "
            "primary."
        ),
        "optimal_low": 2.5, "optimal_high": 7.0,
        "sufficient_low": 1.7, "sufficient_high": 8.6,
        "what_high_means": "Primary hypogonadism (testicular failure) — pituitary trying harder.",
        "what_low_means": (
            "Suppressed pituitary axis: stress, sleep loss, opioids, AAS use "
            "(suppressive), pituitary disease, severe caloric restriction, or "
            "rarely a prolactinoma."
        ),
        "influenced_by": "Pituitary function, exogenous T/AAS, stress, sleep, opioids.",
    },
    "follicle_stimulating_hormone": {
        "title": "Follicle Stimulating Hormone (FSH)",
        "category": "Hormones",
        "unit": "mIU/mL",
        "description": (
            "Pituitary gonadotropin. In males drives Sertoli-cell function "
            "and spermatogenesis. Same axis as LH; usually moves with it."
        ),
        "optimal_low": 1.5, "optimal_high": 5.0,
        "sufficient_low": 1.5, "sufficient_high": 12.4,
        "what_high_means": "Primary testicular failure / impaired spermatogenesis.",
        "what_low_means": "Pituitary suppression (same drivers as LH).",
        "influenced_by": "Same as LH.",
    },
    "estradiol": {
        "title": "Estradiol",
        "category": "Hormones",
        "unit": "pg/mL",
        "description": (
            "Primary estrogen. Adult-male reference is roughly 10-40 pg/mL. "
            "Made from testosterone via aromatase, mostly in adipose tissue. "
            "Important for libido, bone density, lipids — too low or too "
            "high in men is problematic."
        ),
        "optimal_low": 15, "optimal_high": 35,
        "sufficient_low": 10, "sufficient_high": 40,
        "what_high_means": (
            "High body fat (more aromatase), exogenous T without AI, alcohol, "
            "liver dysfunction. Symptoms: water retention, mood, gynecomastia."
        ),
        "what_low_means": (
            "AI overuse, very low body fat, low total T. Symptoms: low libido, "
            "joint dryness, mood depression, bone loss."
        ),
        "influenced_by": "Body fat, alcohol, aromatase activity, total T, AIs.",
    },
    "dehydroepiandrosterone_sulfate": {
        "title": "DHEA Sulfate",
        "category": "Hormones",
        "unit": "mcg/dL",
        "description": (
            "Adrenal androgen precursor. Falls with age (peaks 20s, halves by "
            "50s). Marker of adrenal output."
        ),
        "optimal_low": 250, "optimal_high": 500,
        "sufficient_low": 100, "sufficient_high": 600,
        "what_high_means": "Adrenal hyperplasia, exogenous DHEA supplementation, rare adrenal tumor.",
        "what_low_means": "Adrenal insufficiency, chronic stress / cortisol dominance, aging.",
        "influenced_by": "Age, chronic stress, supplementation.",
    },
    "cortisol": {
        "title": "Cortisol",
        "category": "Hormones",
        "unit": "mcg/dL",
        "description": (
            "Primary stress hormone. Diurnal — peaks early morning, troughs "
            "around midnight. Reference range assumes a morning draw "
            "(7-25 mcg/dL). Single point readings have limited diagnostic "
            "value without context."
        ),
        "optimal_low": 7, "optimal_high": 18,
        "sufficient_low": 5, "sufficient_high": 25,
        "what_high_means": "Acute stress, Cushing's, exogenous steroids, recent intense exercise.",
        "what_low_means": "Adrenal insufficiency, suppression from chronic steroid use.",
        "influenced_by": "Time of draw, sleep, stress, exercise, exogenous steroids, oral contraceptives in females.",
    },
    "thyroid_stimulating_hormone": {
        "title": "Thyroid-Stimulating Hormone (TSH)",
        "category": "Hormones",
        "unit": "mIU/L",
        "description": (
            "Pituitary signal to the thyroid. Inversely related to thyroid "
            "function — high TSH suggests hypothyroidism, low TSH "
            "hyperthyroidism. Optimal narrowly 1-2 mIU/L."
        ),
        "optimal_low": 1.0, "optimal_high": 2.5,
        "sufficient_low": 0.4, "sufficient_high": 4.5,
        "what_high_means": "Subclinical or overt hypothyroidism.",
        "what_low_means": "Hyperthyroidism, exogenous thyroid hormone, pituitary failure.",
        "influenced_by": "Iodine, autoimmunity (Hashimoto, Graves), thyroid meds, severe illness.",
    },

    # ---- Iron metabolism --------------------------------------------------
    "iron": {
        "title": "Iron",
        "category": "Iron Metabolism",
        "unit": "mcg/dL",
        "description": (
            "Serum iron — diurnally variable; should be drawn fasting morning. "
            "Always interpret with ferritin and TIBC, not in isolation."
        ),
        "optimal_low": 70, "optimal_high": 150,
        "sufficient_low": 50, "sufficient_high": 175,
        "what_high_means": "Hemochromatosis, supplementation, transfusion, hemolysis.",
        "what_low_means": "Iron deficiency, chronic disease.",
        "influenced_by": "Time of day, fasting, recent supplementation/intake.",
    },
    "ferritin": {
        "title": "Ferritin",
        "category": "Iron Metabolism",
        "unit": "ng/mL",
        "description": (
            "Iron storage protein — best single marker of body iron stores, "
            "but ALSO an acute-phase reactant. Elevated ferritin can reflect "
            "either iron overload OR inflammation. <30 ng/mL is iron deficient "
            "even if Hb is still normal."
        ),
        "optimal_low": 50, "optimal_high": 200,
        "sufficient_low": 30, "sufficient_high": 300,
        "what_high_means": (
            "Iron overload (hemochromatosis, transfusion), inflammation, "
            "metabolic syndrome / NAFLD, alcohol."
        ),
        "what_low_means": "Iron deficiency — even with normal Hb, treat at <30.",
        "influenced_by": "Inflammation, alcohol, NAFLD, blood loss, iron intake.",
    },
    "total_iron_binding_capacity": {
        "title": "Total Iron-Binding Capacity (TIBC)",
        "category": "Iron Metabolism",
        "unit": "mcg/dL",
        "description": (
            "Capacity of transferrin to carry iron. Rises in iron deficiency, "
            "falls in chronic disease / iron overload."
        ),
        "optimal_low": 250, "optimal_high": 350,
        "sufficient_low": 240, "sufficient_high": 450,
        "what_high_means": "Iron deficiency, pregnancy, oral contraceptives.",
        "what_low_means": "Iron overload, chronic disease, malnutrition.",
        "influenced_by": "Iron status, inflammation, pregnancy, hormones.",
    },
    "iron_percent_saturation": {
        "title": "Iron % Saturation",
        "category": "Iron Metabolism",
        "unit": "% (calc)",
        "description": (
            "Iron / TIBC × 100. <20% suggests iron deficiency; >45% suggests "
            "iron overload — screening threshold for hemochromatosis."
        ),
        "optimal_low": 25, "optimal_high": 40,
        "sufficient_low": 20, "sufficient_high": 45,
        "what_high_means": "Iron overload (hemochromatosis), supplementation, transfusion.",
        "what_low_means": "Iron deficiency.",
        "influenced_by": "Iron stores, recent supplementation.",
    },

    # ---- Vitamins / Other -------------------------------------------------
    "vitamin_d": {
        "title": "Vitamin D",
        "category": "Vitamins & Minerals",
        "unit": "ng/mL",
        "description": (
            "25-hydroxyvitamin D — best measure of vitamin D status. <20 = "
            "deficient; 20-30 = insufficient; 40-60 = optimal for most "
            "endpoints. Toxicity rare below 100."
        ),
        "optimal_low": 40, "optimal_high": 60,
        "sufficient_low": 30, "sufficient_high": 80,
        "what_high_means": "Excess supplementation; >100 ng/mL approaches toxicity.",
        "what_low_means": (
            "Bone, immune, mood implications. Common in low-sun/indoor "
            "lifestyles. Supplementation usually 2000-5000 IU/day."
        ),
        "influenced_by": "Sun exposure, supplementation, skin pigment, body fat, season.",
    },
    "homocysteine": {
        "title": "Homocysteine",
        "category": "Cardiometabolic",
        "unit": "umol/L",
        "description": (
            "Sulfur amino acid intermediate. Elevated levels independently "
            "associate with cardiovascular and cognitive risk. Reduced by "
            "B12, folate, B6 sufficiency."
        ),
        "optimal_low": 0, "optimal_high": 8,
        "sufficient_low": 0, "sufficient_high": 12,
        "what_high_means": "B-vitamin deficiency (B12, folate, B6), MTHFR variants, kidney disease.",
        "what_low_means": "Favorable.",
        "influenced_by": "B12, folate, B6 intake, MTHFR genetics, kidney function.",
    },
}


def all_biomarker_ids() -> list[str]:
    return list(BIOMARKERS.keys())


def get(biomarker_id: str) -> dict | None:
    return BIOMARKERS.get(biomarker_id)
