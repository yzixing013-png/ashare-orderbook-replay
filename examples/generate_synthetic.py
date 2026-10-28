from pathlib import Path
import pandas as pd

def generate(folder):
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    snap={k:0 for k in ['last','pre_close','open','high','low','volume','amount','num_trades','total_bid_vol','total_ask_vol']}
    snap.update({f'{p}{kind}{i}':0 for p in 'ab' for kind in 'pv' for i in range(1,11)})
    snap.update(time=92959000,date=20240207,symbol='SH.TEST',pre_close=10)
    pd.DataFrame([snap]).to_csv(folder/'df_level2.csv',index=False)
    orders=[dict(date=20240207,symbol='SH.TEST',channel=1,index=i+1,order_time='2024-02-07 09:30:00+08:00',side='buy',price=10-i/10,quantity=100,order_type='place') for i in range(11)]
    orders += [dict(orders[0],index=12,side='sell',price=11,quantity=300),dict(orders[0],index=13,order_type='cancel',order_time='2024-02-07 09:30:01+08:00')]
    pd.DataFrame(orders).to_csv(folder/'df_order.csv',index=False)
    pd.DataFrame([dict(date=20240207,symbol='SH.TEST',channel=1,index=1,price=11,quantity=50,trade_time='2024-02-07 09:30:01+08:00',other_info='external(buy)')]).to_csv(folder/'df_trade.csv',index=False)

if __name__=='__main__':
    generate(Path(__file__).resolve().parent/'synthetic')
    print('Synthetic data generated')
