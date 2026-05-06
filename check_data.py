import pandas as pd
df = pd.read_csv('datasets/demand_history.csv')
print('Columns:', df.columns.tolist())
print('Date range:', df.iloc[:,0].min(), 'to', df.iloc[:,0].max())
print('Total rows:', len(df))
print('Unique dates:', df.iloc[:,0].nunique())
