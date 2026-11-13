import pdfplumber
import pandas as pd
import os

# --- CONFIGURATION ---
INPUT_PDF = "KMI-ALL-Re-Composition-as-of-Dec-2025 (1).pdf"  # Rename your PDF to this
OUTPUT_EXCEL = "Ideal_Zero_Debt_Companies.xlsx"

def extract_ideal_companies():
    if not os.path.exists(INPUT_PDF):
        print(f"Error: {INPUT_PDF} not found!")
        return

    all_rows = []
    
    print("Starting Engineering Extraction of 'Ideal' Companies...")

    with pdfplumber.open(INPUT_PDF) as pdf:
        # Tables start from Page 3 to Page 9
        for i in range(2, len(pdf.pages)):
            page = pdf.pages[i]
            table = page.extract_table()
            
            if table:
                # First data page (Page 3) - keep header
                if i == 2:
                    all_rows.extend(table)
                else:
                    # Skip header for subsequent pages
                    all_rows.extend(table[1:])
            print(f"Read Page {i+1}")

    # Convert to DataFrame
    df = pd.DataFrame(all_rows)
    
    # Set headers from the first row of Page 3
    df.columns = df.iloc[0]
    df = df[1:].reset_index(drop=True)

    # --- CLEANING DATA ---
    # We remove '%' and convert to numbers so we can filter for 0
    def clean_ratio(val):
        if val is None: return 100.0 # Ignore empty
        val = str(val).replace('%', '').strip()
        try:
            return float(val)
        except:
            return 100.0 # High value to filter out non-numeric noise

    # Apply cleaning to the three critical columns
    # Column indices for Debt, Investment, Income are usually 4, 5, 6
    df['d_clean'] = df.iloc[:, 4].apply(clean_ratio)
    df['inv_clean'] = df.iloc[:, 5].apply(clean_ratio)
    df['inc_clean'] = df.iloc[:, 6].apply(clean_ratio)

    # --- FILTERING FOR IDEAL COMPANIES ---
    # We look for rows where all three are 0.00
    ideal_df = df[
        (df['d_clean'] == 0.0) & 
        (df['inv_clean'] == 0.0) & 
        (df['inc_clean'] == 0.0)
    ].copy()

    # Drop the temporary cleaning columns
    ideal_df = ideal_df.drop(columns=['d_clean', 'inv_clean', 'inc_clean'])

    # Final formatting
    ideal_df.columns = [
        "No.", "Ticker", "Company Name", "Objective", 
        "Rule 1: Debt Ratio (0%)", "Rule 2: NC Investment (0%)", 
        "Rule 3: NC Income (0%)", "Rule 4: Illiquid Assets (>25%)", 
        "NLA Ratio", "Market Price", "Status"
    ]

    # Add required project headers
    ideal_df["Certifying Body"] = "PSX-KMI Shariah Board (Meezan Bank)"

    # Save to Excel
    ideal_df.to_excel(OUTPUT_EXCEL, index=False)
    
    print("\n" + "="*40)
    print("EXTRACTION COMPLETE")
    print(f"Total Companies Examined: {len(df)}")
    print(f"Ideal (Zero Debt/Interest) Found: {len(ideal_df)}")
    print(f"File Saved: {OUTPUT_EXCEL}")
    print("="*40)
    
    # Show the top ideal tickers in console
    print("\nSample Ideal Tickers:")
    print(ideal_df['Ticker'].tolist())

if __name__ == "__main__":
    extract_ideal_companies()