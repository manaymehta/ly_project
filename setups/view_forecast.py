import pickle
from prophet.plot import plot_plotly

def show_forecast(hosp_id, drug_id, description):
    model_path = f"prophet_models/{hosp_id}_{drug_id}.pkl"
    
    print(f"Loading model for {description} (Hospital: {hosp_id}, Drug: {drug_id})...")
    try:
        with open(model_path, "rb") as f:
            model = pickle.load(f)
    except FileNotFoundError:
        print(f"Error: Could not find model at {model_path}")
        return

    # Forecast 1 year into the future to clearly see seasonal patterns
    future = model.make_future_dataframe(periods=365)
    forecast = model.predict(future)

    # Generate interactive plotly graph
    fig = plot_plotly(model, forecast)
    fig.update_layout(
        title=f"Demand Forecast: {description} [{hosp_id} - {drug_id}]",
        xaxis_title="Date",
        yaxis_title="Units Demanded"
    )
    
    # This will open the graph in your default web browser
    fig.show()

if __name__ == "__main__":
    print("Opening 3 browser tabs with your forecast verifications...\n")
    
    # Check 1: AIIMS Delhi Amoxicillin (Seasonal / Monsoon peak)
    show_forecast("H001", "D004", "AIIMS Delhi Amoxicillin")
    
    # Check 2: Nizam's Insulin (Flat / Chronic year-round)
    show_forecast("H006", "D001", "Nizam's Institute Insulin")
    
    # Check 3 & 4: AIIMS Delhi Salbutamol Inhaler (Seasonal / Winter peak)
    show_forecast("H001", "D017", "AIIMS Delhi Salbutamol Inhaler")
