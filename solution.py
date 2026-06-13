#!/usr/bin/env python3
import sys
import os
import math
from pathlib import Path
import geopandas as gpd

# Verified correct imports from the bhume package
from bhume import load, score, write_predictions
from bhume.baseline import global_median_shift

DEFAULT_VILLAGE = 'data'

def main(village_dir: str) -> None:
    # 1. Load the village bundle
    village = load(village_dir)
    plots = village.plots
    print(f"Processing {village.slug}...")
    
    # 2. Get the baseline shifted positions
    print("Generating baseline median-shift geometries...")
    baseline_preds = global_median_shift(village)
    
    # Map plot_number -> its shifted geometry
    shifted_geoms = {}
    for idx, row in baseline_preds.iterrows():
        plot_id = str(row.get('plot_number', idx))
        shifted_geoms[plot_id] = row.geometry

    smart_rows = []
    print("Executing Multi-Feature Geometric Confidence Calibration Engine...")
    
    # Find median plot size to establish a baseline for scale ranking
    median_map_area = plots['map_area_sqm'].median() if 'map_area_sqm' in plots.columns else 5000.0
    
    # 3. Evaluate each plot row by row
    for idx, plot in plots.iterrows():
        plot_id = str(plot.get('plot_number', idx))
        drawn_area = plot.get('map_area_sqm', 0)
        
        # Calculate Shape Compactness (Isoperimetric Quotient)
        # Formula: 4 * pi * Area / (Perimeter^2)
        # A perfect circle/square is close to 1.0; messy/narrow shapes approach 0.0
        perimeter = plot.geometry.length
        if perimeter > 0:
            compactness = (4 * math.pi * drawn_area) / (perimeter ** 2)
            compactness = min(1.0, max(0.1, compactness))  # Clamp between 0.1 and 1.0
        else:
            compactness = 0.5
        
        # Extract the text record areas
        recorded_area_sqm = plot.get('recorded_area_sqm', None)
        pot_kharaba_ha = plot.get('pot_kharaba_ha', 0)
        if pot_kharaba_ha is None:
            pot_kharaba_ha = 0.0
            
        # Default fallback values
        final_geom = plot.geometry
        status = "flagged"
        confidence = 0.0
        note = "Flagged"
        
        # Get the shifted version of this geometry
        shifted_geom = shifted_geoms.get(plot_id, plot.geometry)
        
        if recorded_area_sqm is None or recorded_area_sqm == 0:
            status = "corrected"
            final_geom = shifted_geom
            # Base neutral confidence scaled slightly down for shape complexity
            confidence = 0.40 * (0.5 + 0.5 * compactness)
            note = "No written record."
        else:
            # Convert Pot-Kharaba hectares to square meters and get total area
            pot_kharaba_sqm = pot_kharaba_ha * 10000.0
            true_total_recorded_sqm = recorded_area_sqm + pot_kharaba_sqm
            
            # Calculate the corrected Area Ratio
            area_ratio = drawn_area / true_total_recorded_sqm
            
            # --- CRITICAL FILTER 1: STRUCTURAL AREA MISMATCH (FLAG IT) ---
            if area_ratio < 0.65 or area_ratio > 1.35:
                status = "flagged"
                final_geom = plot.geometry  # Structural issue: revert to unshifted position
                confidence = 0.0
                note = f"Area structural mismatch ({area_ratio:.2f})."
                
            # --- CRITICAL FILTER 2: GOOD SHAPE MATCH (SHIFT & PRICE CONFIDENCE) ---
            else:
                status = "corrected"
                final_geom = shifted_geom
                
                # Feature A: Distance from a perfect 1.0 area ratio match
                area_deviation = abs(1.0 - area_ratio)
                area_score = max(0.0, 1.0 - (area_deviation * 2.0)) # 1.0 is perfect, drops as deviation grows
                
                # Feature B: Plot Scale Resilience Factor
                # Large plots are mathematically insulated against shift errors.
                # Tiny plots have highly volatile IoUs. Scale score between 0.4 and 1.0.
                scale_ratio = drawn_area / median_map_area
                scale_score = 1.0 - math.exp(-scale_ratio) # Smooth asymptotic curve
                scale_score = max(0.3, min(1.0, scale_score))
                
                # Feature C: Compactness Score (already calculated)
                # Boxier agricultural fields are safer bets than winding ones.
                
                # Blend the features together: Give heavy weight to Area Match, 
                # and use Scale and Compactness as risk-adjusters.
                raw_confidence = (area_score * 0.60) + (scale_score * 0.25) + (compactness * 0.15)
                
                # Map smoothly into the target evaluation bracket (0.15 to 0.98)
                confidence = 0.15 + (raw_confidence * 0.83)
                confidence = max(0.15, min(0.98, confidence))
                
                note = f"Ratio: {area_ratio:.2f} | Scale: {scale_score:.2f} | Comp: {compactness:.2f}"

        smart_rows.append({
            "plot_number": plot_id,
            "status": status,
            "confidence": round(confidence, 4),
            "geometry": final_geom,
            "method_note": note
        })
        
    # 4. Wrap results into a properly projected GeoDataFrame
    smart_preds = gpd.GeoDataFrame(smart_rows, crs=plots.crs)
    
    # 5. Save the output predictions file
    out_path = Path(village_dir) / 'predictions.geojson'
    write_predictions(out_path, smart_preds)
    print(f"Successfully saved multi-feature predictions to: {out_path}")
    
    # 6. Run evaluation script
    print("\n=== ADVANCED EVALUATION RESULTS ===")
    print(score(smart_preds, village))


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_VILLAGE)