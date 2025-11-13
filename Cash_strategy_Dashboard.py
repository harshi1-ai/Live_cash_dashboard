import asyncio 
import pandas as pd
import numpy as np
import motor.motor_asyncio
import logging
import re
from datetime import datetime
import pytz
from threading import Lock
import streamlit as st
import plotly.graph_objects as go
from st_aggrid import AgGrid, GridOptionsBuilder, GridUpdateMode, DataReturnMode
from st_aggrid.shared import JsCode
import plotly.colors
import time
# from streamlit_autorefresh import st_autorefresh
# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# MongoDB URI
mongo_uri = st.secrets["mongo"]["uri"]
class LoginRateLimiter:
    def __init__(self):
        self.attempts = {}
        self.max_attempts = 5
        self.lockout_duration = 900  # 15 minutes
    
    def is_allowed(self, user_id):
        now = time.time()
        if user_id in self.attempts:
            attempts = self.attempts[user_id]
            # Remove old attempts
            attempts = [t for t in attempts if now - t < self.lockout_duration]
            
            if len(attempts) >= self.max_attempts:
                return False
            
            self.attempts[user_id] = attempts
        
        if user_id not in self.attempts:
            self.attempts[user_id] = []
        
        self.attempts[user_id].append(now)
        return True

class Authentication:
    def __init__(self, mongo_uri):
        self.mongo_client = motor.motor_asyncio.AsyncIOMotorClient(mongo_uri)
        self.db = self.mongo_client["OdinApp"]
        self.users_collection = self.db["Users_Details"]  # Your existing collection
    
    async def verify_credentials(self, user_id, password):
        try:
            # Find user in Users_Details collection
            user = await self.users_collection.find_one({"User_ID": user_id})
            
            if user:
                # Check if password matches
                stored_password = user.get("Password", "")
                
                # If passwords are stored as plain text (temporary)
                if stored_password == password:
                    return True
                
                # If passwords are hashed (recommended for production)
                # import bcrypt
                # return bcrypt.checkpw(password.encode('utf-8'), stored_password.encode('utf-8'))
                
            return False
        except Exception as e:
            logger.error(f"Authentication error: {e}")
            return False
    
    async def get_user_details(self, user_id):
        """Get additional user details if needed"""
        try:
            user = await self.users_collection.find_one({"User_ID": user_id})
            return user
        except Exception as e:
            logger.error(f"Error fetching user details: {e}")
            return None

class StreamlitDashboard:
    def __init__(self, mongo_uri: str):
        self.date = datetime.now(pytz.timezone("Asia/Kolkata")).strftime("%Y-%m-%d %I:%M %p IST")
        self.token_list = []
        self.file_lock = Lock()
        self.mongo_client = motor.motor_asyncio.AsyncIOMotorClient(mongo_uri)
        self.db = self.mongo_client["OdinApp"]
        self.position_col = self.db["Strategies_Trade_Data"]
        self.auth = Authentication(mongo_uri)
        self.rate_limiter = LoginRateLimiter()
        logger.info("Initialized StreamlitDashboard for dashboard")


    async def fetch_ltp(self, token: str, strategy_id: str, client_id: str, instrument: str = None) -> tuple[float, float]:
        try:
            ltp = float('nan')
            close = float('nan')
            
            # First: Try to get Close from PrevClose in strategies trade data
            try:
                prev_close_doc = await self.position_col.find_one(
                    {
                        "ExchangeInstrumentID": int(token),
                        "StrategyID": strategy_id
                    },
                    {"PrevClose": 1}
                )
                if prev_close_doc and "PrevClose" in prev_close_doc and not pd.isna(prev_close_doc["PrevClose"]):
                    close = prev_close_doc["PrevClose"]
                    logger.info(f"Fetched Close {close} from PrevClose in Strategies_Trade_Data for token {token} (strategy: {strategy_id})")
                else:
                    logger.info(f"No valid PrevClose found in trade data for token {token} (strategy: {strategy_id}); will fallback to LiveLtp for Close")
            except Exception as e:
                logger.error(f"Error fetching PrevClose for token {token}, strategy {strategy_id}: {e}")
            
            # Always fetch LTP from LiveLtp (and Close fallback if needed)
            ltp_doc = await self.db["LiveLtp"].find_one({"ExchangeInstrumentID": int(token)})
            if ltp_doc:
                ltp = ltp_doc.get("LastTradedPrice", float('nan'))
                if pd.isna(close):  # Only override if no PrevClose was set
                    close = ltp_doc.get("Close", float('nan'))
                    logger.info(f"Fetched LTP {ltp} and Close {close} from LiveLtp for token {token}")
                else:
                    logger.info(f"Fetched LTP {ltp} from LiveLtp for token {token} (using PrevClose for Close)")
            else:
                logger.warning(f"No LiveLtp data found for token {token}")
            
            # Ensure close is a numeric float (handles any lingering NaNs or non-numeric)
            if pd.isna(close) or not isinstance(close, (int, float)):
                close = float('nan')
            
            return ltp, close
        except Exception as e:
            logger.error(f"Error in fetch_ltp for token {token}: {e}")
            return float('nan'), float('nan')

    async def stats_calculation(self, output_df: pd.DataFrame, total_fund: float) -> tuple[pd.DataFrame, pd.DataFrame]:
        try:
            output_df = output_df.copy()
            output_df["LTP"] = pd.to_numeric(output_df["LTP"], errors="coerce")
            output_df["Close"] = pd.to_numeric(output_df["Close"], errors="coerce")
            def ensure_list_of_floats(val):
                if isinstance(val, list):
                    return [float(x) for x in val if isinstance(x, (int, float, str)) and str(x).replace('.', '', 1).replace('-', '', 1).isdigit()]
                if isinstance(val, str):
                    parts = re.split(r'[, s]+', val.strip('[]()'))
                    return [float(x) for x in parts if x and str(x).replace('.', '', 1).replace('-', '', 1).isdigit()]
                if isinstance(val, (int, float)):
                    return [float(val)]
                return []
            list_columns = ["BuyPrice", "SellPrice", "ExQty", "EnQty"]
            for col in list_columns:
                output_df[col] = output_df[col].apply(ensure_list_of_floats)
            output_df["CapitalUsed"] = output_df.apply(
                lambda row: sum(p * q for p, q in zip(row["BuyPrice"], row["EnQty"]) if p and q), axis=1)
            buy_turnover = sum(sum(p * q for p, q in zip(row["BuyPrice"], row["EnQty"]) if p and q) for _, row in output_df.iterrows())
            sell_turnover = sum(sum(p * q for p, q in zip(row["SellPrice"], row["ExQty"]) if p and q) for _, row in output_df.iterrows())
            total_turnover = buy_turnover + sell_turnover
            output_df["Profit"] = output_df.apply(
                lambda row: (
                    (row["LTP"] - (np.mean(row["BuyPrice"]) if row["BuyPrice"] else 0)) * (sum(row["EnQty"]) if row["EnQty"] else 0)
                    + sum((p_s - p_b) * q for p_s, p_b, q in zip(row["SellPrice"], row["BuyPrice"], row["ExQty"]) if p_s and p_b and q)
                    if not pd.isna(row["LTP"])
                    else sum((p_s - p_b) * q for p_s, p_b, q in zip(row["SellPrice"], row["BuyPrice"], row["ExQty"]) if p_s and p_b and q)
                ), axis=1)
            output_df["UnrealisedMTM"] = output_df.apply(
                lambda row: (
                    (row["LTP"] - (np.mean(row["BuyPrice"]) if row["BuyPrice"] else 0)) * row["RemainingQty"]
                    if not pd.isna(row["LTP"]) else 0
                ), axis=1)
                 
            profits = output_df["Profit"]
            output_df["BuyPriceAvg"] = output_df["BuyPrice"].apply(lambda x: round(np.mean(x), 2) if x else np.nan)
            output_df["SellPriceAvg"] = output_df["SellPrice"].apply(lambda x: round(np.mean(x),2) if x else np.nan)
            profit_trades = profits[profits >= 0]
            loss_trades = profits[profits < 0]
            stats = {
                "Investment": total_fund,
                "Number of Open Positions": output_df["Pos"].value_counts().get("open", 0),
                "Number of Closed Positions": output_df["Pos"].value_counts().get("close", 0),
                "Total Profit": profit_trades.sum(),
                "Total Loss": loss_trades.sum(),
                "Overall Profit": profits.sum(),
                "Profit Positions": len(profit_trades),
                "Loss Positions": len(loss_trades),
                "Avg Profit": profit_trades.mean() if len(profit_trades) > 0 else 0,
                "Avg Loss": loss_trades.mean() if len(loss_trades) > 0 else 0,
                "Total Trades": len(profits),
                "Profit Factor": abs(profit_trades.sum() / loss_trades.sum()) if loss_trades.sum() != 0 else float("inf"),
                "Capital Utilization(Open)": output_df.loc[output_df["Pos"] == "open", "CapitalUsed"].sum(),
                # "Capital Utilization %": (output_df.loc[output_df["Pos"] == "open", "CapitalUsed"].sum() / total_fund * 100),
                "Turnover": total_turnover
            }
            def format_value(x):
                if isinstance(x, (int, float)) and not pd.isna(x):
                    return f"{x:.2f}"
                return x
            stats_df = pd.DataFrame([(k, format_value(v)) for k, v in stats.items()], columns=pd.Index(["Metric", "Value"]))
            return output_df, stats_df
        except Exception as e:
            logger.error(f"Error in stats_calculation: {e}")
            try:
                import streamlit as st
                st.error(f"Error in stats_calculation: {e}")
                st.write("DataFrame at error:", output_df.head())
            except Exception:
                pass
            return output_df, pd.DataFrame()

    async def fetch_all_positions(self):
        logger.info("Fetching all positions from MongoDB")
        all_positions = pd.DataFrame(await self.position_col.find().to_list(length=None))
        return all_positions

    async def close(self):
        self.mongo_client.close()
        logger.info("MongoDB client closed")

    async def fetch_and_process_data(self, client_ids, strategy_ids):
        try:
            all_positions = await self.fetch_all_positions()
            if all_positions.empty:
                return None, None, None, "No positions found in the database."

            def extract_client_id(identifier):
                if isinstance(identifier, str):
                    space_idx = identifier.find(' ') if ' ' in identifier else len(identifier)
                    underscore_idx = identifier.find('_') if '_' in identifier else len(identifier)
                    split_idx = min(space_idx, underscore_idx)
                    return identifier[:split_idx] if split_idx > 0 else identifier
                return "UNKNOWN"

            all_positions["ClientID"] = all_positions["Identifier"].apply(extract_client_id)
            all_positions["StrategyID"] = all_positions["StrategyID"].astype(str)
            all_positions["MaxHigh"]=all_positions["MaxHigh"].astype(str)
            all_positions["StopLoss"] = all_positions["StopLoss"].apply(
lambda x: f"{x:.2f}" if pd.notna(x) else "-")

            client_df = all_positions[all_positions["ClientID"].isin(client_ids)]
            if client_df.empty:
                return None, None, None, "No data found for the entered Client IDs."

            filtered_df = client_df[client_df["StrategyID"].isin(strategy_ids)] if strategy_ids else client_df
            if filtered_df.empty:
                return None, None, None, "No data found for the selected strategies and clients."

            # Create a copy to avoid SettingWithCopyWarning
            filtered_df = filtered_df.copy()

            async def return_value(val):
                return val

            async def get_ltp_list(df):
                tasks = [
                        self.fetch_ltp(str(row["ExchangeInstrumentID"]), str(row["StrategyID"]), row["ClientID"], row.get("Instrument")) if row["Pos"] == "open" 
                        else return_value((row.get("LTP", np.nan), row.get("Close", np.nan)))
                        for _, row in df.iterrows()
                    ]
                results = await asyncio.gather(*tasks)
                ltp_list = [result[0] for result in results]
                close_list = [result[1] for result in results]
                return ltp_list, close_list

            ltp_close = await get_ltp_list(filtered_df)
            filtered_df["LTP"] = ltp_close[0]
            filtered_df["Close"] = ltp_close[1]



            
            def update_sellprice_with_ltp(row):
                if row["Pos"] == "open":
                    sell_prices = row["SellPrice"]
                    if not isinstance(sell_prices, list):
                        sell_prices = [] if pd.isna(sell_prices) else [sell_prices]
                    ltp = row.get("LTP", np.nan)
                    if pd.isna(ltp):
                        return sell_prices
                    if sell_prices:
                        sell_prices = sell_prices[:-1] + [ltp]
                    else:
                        sell_prices = [ltp]
                    return sell_prices
                return row["SellPrice"]

            filtered_df["SellPrice"] = filtered_df.apply(update_sellprice_with_ltp, axis=1)
            filtered_df["Qty"] = filtered_df["EnQty"].apply(
                lambda x: sum(x) if isinstance(x, list) and len(x) > 0 else (x if isinstance(x, (int, float)) else 0)
            )

          
            if "StockInvestment" in filtered_df.columns and not filtered_df["StockInvestment"].dropna().empty:
                total_fund = float(filtered_df["StockInvestment"].dropna().sum())  # Sum all non-null values
                logger.info(f"Total fund from StockInvestment: {total_fund}")
            else:
                # Option 2: Fallback to sum of BuyPrice * EnQty (full lists)
                total_fund = filtered_df.apply(
                    lambda row: sum(float(p) * float(q) for p, q in zip(
                        row.get("BuyPrice", []) if isinstance(row.get("BuyPrice"), list) else [row.get("BuyPrice")] if pd.notna(row.get("BuyPrice")) else [],
                        row.get("EnQty", []) if isinstance(row.get("EnQty"), list) else [row.get("EnQty")] if pd.notna(row.get("EnQty")) else []
                    ) if pd.notna(p) and pd.notna(q)) * float(row.get("LotSize", 1)), axis=1
                ).sum()
                logger.info(f"Total fund from BuyPrice*EnQty: {total_fund}")

            # Ensure non-negative scalar
            total_fund = max(0.0, float(total_fund))

            filtered_df, stats_df = await self.stats_calculation(filtered_df, total_fund)
            def calculate_yesterdays_pnl(row):
                if row["Pos"] == "open":
                    close = row.get("Close", np.nan)
                    if pd.isna(close):
                        return 0.0
                    buy_mean = np.mean(row["BuyPrice"]) if row["BuyPrice"] else 0
                    qty = row.get("Qty", 0)
                    return (close - buy_mean) * qty
                else:
                    return row.get("Profit", np.nan)
            filtered_df["Yesterday's PNL"] = filtered_df.apply(calculate_yesterdays_pnl, axis=1)

            def extract_entry_date(row):
                en_dtime = row.get("EnDTime", None)
                if isinstance(en_dtime, list) and len(en_dtime) > 0:
                    date_str = en_dtime[0]
                elif isinstance(en_dtime, str):
                    date_str = en_dtime
                else:
                    return pd.NaT
                try:
                    return pd.to_datetime(date_str, errors='coerce')
                except Exception:
                    return pd.NaT

            filtered_df["TradeDate"] = filtered_df.apply(extract_entry_date, axis=1)
            filtered_df = filtered_df.dropna(subset=["TradeDate"]).sort_values("TradeDate").reset_index(drop=True)

            return filtered_df, stats_df, total_fund, None
        except Exception as e:
            logger.error(f"Error in fetch_and_process_data: {e}")
            return None, None, None, f"Error processing data: {e}"

    def render_kpi_card(self, label, value, value_class="", style="", title=None):
        title_attr = f' title="{title or label}"' if (title or label) else ''
        return (
            f'<div class="sticky-kpi-card"{title_attr}>'
            f'<div class="kpi-label">{label}</div>'
            f'<div class="kpi-value {value_class}" style="{style}">{value}</div>'
            f'</div>'
        )

    def render_small_kpi_card(self, label, value, kpi_class="", title=None):
        title_attr = f' title="{title or label}"' if (title or label) else ''
        return (
            f'<div class="small-kpi-card {kpi_class}"{title_attr}>'
            f'<div class="kpi-label">{label}</div>'
            f'<div class="kpi-value">{value}</div>'
            f'</div>'
        )

    def render_bar_chart(self, profit_by_strategy, bar_colors):
        fig = go.Figure(
            data=[
                go.Bar(
                    x=profit_by_strategy["StrategyID"],
                    y=profit_by_strategy["Profit"],
                    marker_color=bar_colors,
                    text=profit_by_strategy["Profit"].round(2),
                    textposition='outside',
                    width=0.1
                )
            ]
        )
        fig.add_hline(y=0, line_dash="dash", line_color="var(--text-color)", opacity=0.6)
        fig.update_layout(
            xaxis_title="Strategy ID",
            yaxis_title="Profit (INR)",
            font=dict(family="Inter, sans-serif", color="var(--text-color)", size=12),
            plot_bgcolor="var(--secondary-background-color)",
            paper_bgcolor="var(--background-color)",
            xaxis=dict(tickangle=45, zeroline=True, zerolinewidth=1, zerolinecolor='var(--border-color)'),
            yaxis=dict(zeroline=True, zerolinewidth=1, zerolinecolor='var(--border-color)'),
            margin=dict(l=40, r=40, t=40, b=80),
            showlegend=False
        )
        return fig

    async def show_login_page(self):
        st.markdown("""
        <div style="text-align:center; margin-top:10px;">
            <h1 style="color:#059669; font-size:2.5rem; font-family:'Inter', sans-serif;">Live Cash Strategy Dashboard</h1>
            <p style="font-size:1.2rem; margin-bottom:2rem; color:var(--text-color);">Please login to continue</p>
        </div>
        """, unsafe_allow_html=True)
        
        # Center the login form
        col1, col2, col3 = st.columns([1, 2, 1])
        with col2:
            st.markdown('<div class="login-container">', unsafe_allow_html=True)
            
            with st.form("login_form", clear_on_submit=False):
                st.markdown('<div style="text-align:center;"><h3>Login</h3></div>', unsafe_allow_html=True)
                user_id = st.text_input("User ID", autocomplete="off", key="login_user_id")
                password = st.text_input("Password", type="password", autocomplete="off", key="login_password")
                
                # Only the Streamlit submit button for login
                submit_clicked = st.form_submit_button("LOGIN", type="primary", use_container_width=True, help="")
                
                # Handle form submission logic
                if submit_clicked:
                    if not user_id or not password:
                        st.error("Please enter both User ID and Password")
                    else:
                        # Check rate limiting
                        if not self.rate_limiter.is_allowed(user_id):
                            st.error("Too many login attempts. Please try again in 15 minutes.")
                        else:
                            with st.spinner("Verifying credentials..."):
                                if await self.auth.verify_credentials(user_id, password):
                                    st.session_state.authenticated = True
                                    st.session_state.user_id = user_id
                                    st.session_state.client_id = user_id  # Set client_id for dashboard
                                    st.success("Login successful!")
                                    time.sleep(1)  # Show success message briefly
                                    st.rerun()
                                else:
                                    st.error("Invalid User ID or Password")
            st.markdown('</div>', unsafe_allow_html=True)
            
            # Add JavaScript to force gradient on login button
            st.markdown("""
            <script>
            function applyGradient() {
                var buttons = document.querySelectorAll('.stButton > button');
                buttons.forEach(function(button) {
                    button.style.background = 'linear-gradient(135deg, #059669 0%, #10B981 50%, #34D399 100%)';
                    button.style.backgroundImage = 'linear-gradient(135deg, #059669 0%, #10B981 50%, #34D399 100%)';
                    button.style.backgroundColor = 'transparent';
                    button.style.color = 'white';
                    button.style.border = 'none';
                    button.style.borderRadius = '8px';
                    button.style.padding = '12px 24px';
                    button.style.fontSize = '1.1rem';
                    button.style.fontWeight = '600';
                    button.style.textTransform = 'uppercase';
                    button.style.letterSpacing = '0.5px';
                    button.style.boxShadow = '0 4px 15px rgba(5, 150, 105, 0.3)';
                });
            }
            
            // Apply gradient multiple times to ensure it sticks
            setTimeout(applyGradient, 500);
            setTimeout(applyGradient, 1000);
            setTimeout(applyGradient, 2000);
            setTimeout(applyGradient, 3000);
            
            // Also apply on any DOM changes
            var observer = new MutationObserver(applyGradient);
            observer.observe(document.body, { childList: true, subtree: true });
            </script>
            """, unsafe_allow_html=True)

    async def run_dashboard_async(self):
        # st_autorefresh(interval=60 * 1000, key="datarefresh")
        st.set_page_config(layout="wide", page_title=" Live Cash Strategy Dashboard", page_icon=None)

        # Initialize authentication session state
        if 'authenticated' not in st.session_state:
            st.session_state.authenticated = False
        if 'user_id' not in st.session_state:
            st.session_state.user_id = None
        if 'client_id' not in st.session_state:
            st.session_state.client_id = None

        # Check authentication first
        if not st.session_state.get('authenticated', False):
            await self.show_login_page()
            return

        # --- Auto-refresh every 1 minute (60 seconds) ---
        # (Removed manual timer and rerun logic)
        # Updated CSS with enhanced title visibility and table alignment
        st.markdown("""
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
        :root {
            --primary-color: #1E40AF;
            --secondary-color: #059669;
            --background-color: #F9FAFB;
            --secondary-background-color: #FFFFFF;
            --text-color: #111827;
            --border-color: #D1D5DB;
            --card-shadow: 0 2px 8px rgba(0, 0, 0, 0.1);
            --hover-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
        }
        
        /* Login page styling */
        .login-container {
            background: var(--secondary-background-color);
            border-radius: 12px;
            padding: 2rem;
            box-shadow: var(--card-shadow);
            border: 1px solid var(--border-color);
        }
        
        .login-form input {
            border: 1px solid var(--border-color);
            border-radius: 6px;
            padding: 10px;
            font-size: 1rem;
        }
        
        .login-form button, .stButton > button {
            background: linear-gradient(135deg, #059669 0%, #10B981 50%, #34D399 100%) !important;
            color: white !important;
            border: none !important;
            border-radius: 8px !important;
            padding: 12px 24px !important;
            font-size: 1.1rem !important;
            font-weight: 600 !important;
            cursor: pointer !important;
            transition: all 0.3s ease !important;
            box-shadow: 0 4px 15px rgba(5, 150, 105, 0.3) !important;
            text-transform: uppercase !important;
            letter-spacing: 0.5px !important;
        }
        
        .login-form button:hover, .stButton > button:hover {
            background: linear-gradient(135deg, #047857 0%, #059669 50%, #10B981 100%) !important;
            transform: translateY(-2px) !important;
            box-shadow: 0 6px 20px rgba(5, 150, 105, 0.4) !important;
        }
        
        .login-form button:active, .stButton > button:active {
            transform: translateY(0) !important;
            box-shadow: 0 2px 10px rgba(5, 150, 105, 0.3) !important;
        }
        
        /* Override Streamlit's default button styles */
        .stButton > button[data-testid="baseButton-secondary"] {
            background: linear-gradient(135deg, #059669 0%, #10B981 50%, #34D399 100%) !important;
            color: white !important;
        }
        
        /* Force gradient on all Streamlit buttons */
        .stButton > button,
        .stButton > button[data-testid="baseButton-secondary"],
        .stButton > button[data-testid="baseButton-primary"],
        .stButton > button[data-testid="baseButton-tertiary"],
        .stButton > button[data-testid="baseButton-success"],
        .stButton > button[data-testid="baseButton-warning"],
        .stButton > button[data-testid="baseButton-danger"],
        .stButton > button[data-testid="baseButton-info"],
        .stButton > button[data-testid="baseButton-light"],
        .stButton > button[data-testid="baseButton-dark"] {
            background: linear-gradient(135deg, #059669 0%, #10B981 50%, #34D399 100%) !important;
            background-image: linear-gradient(135deg, #059669 0%, #10B981 50%, #34D399 100%) !important;
            color: white !important;
            border: none !important;
            border-radius: 8px !important;
            padding: 12px 24px !important;
            font-size: 1.1rem !important;
            font-weight: 600 !important;
            text-transform: uppercase !important;
            letter-spacing: 0.5px !important;
            box-shadow: 0 4px 15px rgba(5, 150, 105, 0.3) !important;
        }
        
        /* Override any inline styles */
        .stButton > button[style*="background"] {
            background: linear-gradient(135deg, #059669 0%, #10B981 50%, #34D399 100%) !important;
            background-image: linear-gradient(135deg, #059669 0%, #10B981 50%, #34D399 100%) !important;
        }
        
        /* Additional specificity for form buttons */
        form .stButton > button,
        .login-container .stButton > button {
            background: linear-gradient(135deg, #059669 0%, #10B981 50%, #34D399 100%) !important;
            background-image: linear-gradient(135deg, #059669 0%, #10B981 50%, #34D399 100%) !important;
            color: white !important;
        }
        
        /* ULTIMATE OVERRIDE - Force gradient on ALL buttons */
        .stButton > button,
        .stButton > button *,
        .stButton > button::before,
        .stButton > button::after,
        .stButton > button:hover,
        .stButton > button:active,
        .stButton > button:focus,
        .stButton > button:visited {
            background: linear-gradient(135deg, #059669 0%, #10B981 50%, #34D399 100%) !important;
            background-image: linear-gradient(135deg, #059669 0%, #10B981 50%, #34D399 100%) !important;
            background-color: transparent !important;
            color: white !important;
            border: none !important;
            border-radius: 8px !important;
            padding: 12px 24px !important;
            font-size: 1.1rem !important;
            font-weight: 600 !important;
            text-transform: uppercase !important;
            letter-spacing: 0.5px !important;
            box-shadow: 0 4px 15px rgba(5, 150, 105, 0.3) !important;
        }
        
        /* Override any CSS variables */
        .stButton > button {
            --background-color: transparent !important;
            --color: white !important;
        }
        @media (prefers-color-scheme: dark) {
            :root {
                --primary-color: #60A5FA;
                --secondary-color: #34D399;
                --background-color: #1F2937;
                --secondary-background-color: #374151;
                --text-color: #F9FAFB;
                --border-color: #4B5563;
                --card-shadow: 0 2px 8px rgba(0, 0, 0, 0.3);
                --hover-shadow: 0 4px 12px rgba(0, 0, 0, 0.4);
            }
        }
        /* Bounce animation keyframes */
        @keyframes bounce {
            0%, 100% { transform: scale(1); }
            20% { transform: scale(1.08); }
            40% { transform: scale(1.15); }
            60% { transform: scale(1.08); }
            80% { transform: scale(1.03); }
        }
        .sticky-kpi-card:hover, .small-kpi-card:hover, .bounce-on-hover:hover {
            animation: bounce 0.7s;
        }
        .stApp {
            background-color: var(--background-color);
            font-family: 'Inter', sans-serif;
            color: var(--text-color);
            padding-top: 28px !important; /* Match the new header height */
        }
        .main-title {
            position: fixed !important;
            top: 0;
            left: 0;
            width: 100vw;
            z-index: 3000;
            background: var(--secondary-background-color);
            font-size: 1.05rem;
            font-weight: 700;
            padding: 4px 0 4px 0;
            border-radius: 0 0 14px 14px;
            # box-shadow: var(--card-shadow);
            # border-bottom: 2px solid var(--border-color);
            text-align: center;
            margin: 40px 0 0 0; /* Move main title down below deploy bar */
            display: block;
        }
        section[data-testid="stSidebar"] {
            background-color: var(--secondary-background-color);
            padding: 20px;
            border-right: 1px solid var(--border-color);
        }
        .kpi-row {
            display: flex;
            flex-wrap: wrap;
            justify-content: center;
            gap: 10px;
            margin-bottom: 1.2rem;
            margin-top: 0.7rem; /* Space after fixed title */
        }
        .kpi-card {
            background: var(--secondary-background-color);
            border-radius: 8px;
            padding: 6px 6px;
            box-shadow: var(--card-shadow);
            text-align: center;
            transition: transform 0.2s ease, box-shadow 0.2s ease;
            border: 1px solid var(--border-color);
            background: linear-gradient(to bottom, rgba(0, 123, 255, 0.06), rgba(0, 123, 255, 0.03)), #ffffff;
            flex: 1 1 160px;
            max-width: 220px;
            min-width: 80px;
        }
        .kpi-card:hover {
            transform: translateY(-2px);
            box-shadow: var(--hover-shadow);
        }
        .kpi-label {
            font-size: 0.85rem;
            color: var(--text-color);
            font-weight: 500;
            margin-bottom: 0.4rem;
        }
        .kpi-value {
            font-size: 1rem;
            font-weight: 600;
            padding: 1px 0;
        }
        .kpi-value.investment, .kpi-value.open, .kpi-value.closed {
            color: #000000; /* Black color for Investment, Open Positions, Closed Positions */
        }
        .kpi-value.profit {
            color: #059669; /* Keep green for Total Profit */
        }
        .kpi-value.loss {
            color: #DC2626; /* Keep red for Total Loss */
        }
        .section-heading {
            font-size: 1.55rem;
            font-weight: 600;
            margin: 1.5rem 0 0.75rem;
        }
        .custom-hr {
            border: none;
            border-top: 1px solid var(--border-color);
            margin: 1.5rem 0;
        }
        .stDataFrame {
            border: 1px solid var(--border-color);
            border-radius: 8px;
            overflow: hidden;
        }
        .stDataFrame thead tr th {
            background-color: var(--secondary-background-color);
            color: var(--text-color);
            font-size: 0.98rem;
            font-weight: 600;
            border-bottom: 1px solid var(--border-color);
            text-align: center;
            padding: 10px;
        }
        .stDataFrame tbody tr {
            border-bottom: 1px solid var(--border-color);
        }
        .stDataFrame tbody tr:nth-child(even) {
            background-color: rgba(0, 0, 0, 0.02);
        }
        .stDataFrame tbody tr:hover {
            background-color: rgba(0, 0, 0, 0.04);
        }
        .profit-positive {
            background-color: var(--secondary-color);
            color: #fff;
            font-weight: 500;
        }
        .profit-negative {
            background-color: #DC2626;
            color: #fff;
            font-weight: 500;
        }
        section[data-testid="stSidebar"] input,
        section[data-testid="stSidebar"] .stMultiSelect,
        section[data-testid="stSidebar"] .stSelectbox {
            border: 1px solid var(--border-color);
            background: var(--secondary-background-color);
            color: var(--text-color);
            border-radius: 6px;
            padding: 8px;
            font-size: 0.95rem;
        }
        section[data-testid="stSidebar"] .stMultiSelect div[data-baseweb="select"] > div {
            border: 1px solid var(--border-color);
            background: var(--secondary-background-color);
            color: var(--text-color);
            border-radius: 6px;
            min-height: 36px;
        }
        section[data-testid="stSidebar"] .stMultiSelect [data-baseweb="tag"] {
            background: var(--background-color);
            color: var(--text-color);
            border: 1px solid var(--border-color);
            border-radius: 4px;
            font-size: 0.9rem;
            padding: 4px 8px;
        }
        .stButton>button {
            background-color: var(--primary-color);
            color: #FFFFFF;
            border-radius: 6px;
            padding: 8px 16px;
            font-weight: 500;
            border: none;
            transition: background-color 0.2s ease;
        }
        .stButton>button:hover {
            background-color: #2563EB;
        }
        .stTabs [data-baseweb="tab-list"] {
            border-bottom: 1px solid var(--border-color) !important;
            justify-content: center !important;
            display: flex !important;
        }
        .stTabs [data-baseweb="tab"] {
            font-size: 5.6rem !important;
            font-weight: 900 !important;
            font-family: 'Inter', 'Segoe UI', 'Arial', sans-serif !important;
            padding: 36px 80px !important;
            color: #000000 !important;
            text-align: center !important;
            font-family: 'Inter', sans-serif !important;
            box-shadow: 0 2px 8px rgba(30, 64, 175, 0.08) !important;
            letter-spacing: 0.01em !important;
        }
        .stTabs [data-baseweb="tab"][aria-selected="true"] {
            background-color: var(--secondary-background-color) !important;
            color: #000000 !important;
            border-bottom: 8px solid #059669 !important;
            box-shadow: 0 4px 16px rgba(30, 64, 175, 0.12) !important;
        }
        .ag-theme-streamlit .ag-cell {
            text-align: center !important;
            padding: 14px !important;
            font-size: 1rem !important;
            white-space: normal !important; /* Prevent text cutoff */
        }
        .ag-theme-streamlit .ag-row {
            border-bottom: 1px solid var(--border-color) !important;
        }
        .ag-theme-streamlit .ag-row:nth-child(even) {
            background-color: rgba(0, 0, 0, 0.02) !important;
        }
        .ag-theme-streamlit .ag-row:hover {
            background-color: rgba(0, 0, 0, 0.04) !important;
        }
        .performance-controls-row .stMultiSelect, .performance-controls-row .stDateInput, .performance-controls-row .stCheckbox {
            font-size: 0.98rem !important;
            padding: 2px 6px !important;
            min-height: 32px !important;
            margin-bottom: 0 !important;
        }
        .performance-controls-row .stCheckbox {
            margin-top: 18px !important;
        }
        .performance-controls-row label, .performance-controls-row .stMultiSelect label, .performance-controls-row .stDateInput label {
            font-size: 0.98rem !important;
            margin-bottom: 2px !important;
        }
        .performance-controls-row {
            margin-bottom: 0.5rem !important;
        }
        .tab-desc-box {
            background: var(--secondary-background-color);
            border: 1px solid var(--border-color);
            border-radius: 7px;
            padding: 7px 14px 6px 14px;
            margin: 10px auto 10px auto;
            max-width: 600px;
            font-size: 0.98rem;
            color: var(--text-color);
            box-shadow: 0 2px 6px rgba(30, 64, 175, 0.06), 0 1px 3px rgba(0,0,0,0.04);
            text-align: center;
        }
        /* Bold and larger font for AgGrid headers */
        .ag-theme-streamlit .ag-header-cell-label {
            font-size: 1.15rem !important;
            font-weight: 900 !important;
            color: #000000 !important;
            padding: 10px 8px !important;
        }
        /* Strongest override for AgGrid header text color */
        .ag-theme-streamlit .ag-header-cell, 
        .ag-theme-streamlit .ag-header-cell-label, 
        .ag-theme-streamlit .ag-header-cell-label span, 
        .ag-theme-streamlit .ag-header-cell-text, 
        .ag-theme-streamlit .ag-header-group-cell-label, 
        .ag-theme-streamlit .ag-header-group-cell-label span {
            color: #000000 !important;
            font-weight: 900 !important;
        }
        /* Larger font and more padding for AgGrid cells */
        .ag-theme-streamlit .ag-cell {
            font-size: 1.08rem !important;
            padding: 12px 8px !important;
        }
        /* Sticky header for AgGrid */
        .ag-theme-streamlit .ag-header {
            position: sticky !important;
            top: 0;
            z-index: 10;
            background: var(--secondary-background-color) !important;
        }
        .sticky-kpi-row {
            position: fixed;
            top: 116px; /* Adjust if your main-title height changes */
            left: 0;
            width: 100vw;
            z-index: 2999;
            background: var(--secondary-background-color);
            box-shadow: var(--card-shadow);
            border-bottom: 1.5px solid var(--border-color);
            padding: 8px 0 8px 0;
            display: flex;
            justify-content: center;
            gap: 18px;
        }
        .sticky-kpi-card {
            background: var(--secondary-background-color);
            border-radius: 8px;
            padding: 8px 18px;
            box-shadow: var(--card-shadow);
            text-align: center;
            border: 1px solid var(--border-color);
            font-size: 1.1rem;
            font-weight: 600;
            min-width: 180px;
            background: linear-gradient(to bottom, rgba(0, 123, 255, 0.06), rgba(0, 123, 255, 0.03)), #ffffff;
        }
        .small-kpi-row {
            display: flex;
            flex-wrap: wrap;
            justify-content: center;
            gap: 8px;
            margin-bottom: 0.7rem;
            margin-top: 0.2rem;
        }
        .small-kpi-card {
            background: var(--secondary-background-color);
            border-radius: 6px;
            padding: 4px 8px;
            box-shadow: var(--card-shadow);
            text-align: center;
            border: 1px solid var(--border-color);
            font-size: 0.92rem;
            font-weight: 500;
            min-width: 110px;
            max-width: 180px;
            margin: 2px 2px;
            background: linear-gradient(to bottom, rgba(0, 123, 255, 0.06), rgba(0, 123, 255, 0.03)), #ffffff;
        }
        .small-kpi-card.profit {
            color: #059669;
        }
        .small-kpi-card.loss {
            color: #DC2626;
        }
        /* Force green gradient on the Streamlit LOGIN button */
        .stButton > button {
            background: linear-gradient(90deg, #059669 0%, #10B981 100%) !important;
            color: #fff !important;
            border: none !important;
            border-radius: 8px !important;
            padding: 14px 48px !important;
            font-size: 1.2rem !important;
            font-weight: 700 !important;
            text-transform: uppercase !important;
            letter-spacing: 0.5px !important;
            box-shadow: 0 4px 15px rgba(5, 150, 105, 0.18) !important;
            cursor: pointer !important;
            transition: all 0.2s !important;
            font-family: 'Inter', sans-serif !important;
        }
        .stButton > button:hover {
            background: linear-gradient(90deg, #10B981 0%, #059669 100%) !important;
            color: #fff !important;
            transform: translateY(-2px) !important;
        }
        </style>
        """, unsafe_allow_html=True)

        # Initialize session state
        if 'client_id_input' not in st.session_state:
            st.session_state.client_id_input = ''
        if 'selected_strategy' not in st.session_state:
            st.session_state.selected_strategy = None
        if 'data_fetched' not in st.session_state:
            st.session_state.data_fetched = False
        if 'strategy_ids' not in st.session_state:
            st.session_state.strategy_ids = []

        # Sidebar
        with st.sidebar:
            # Logout button at the top
            if st.button("Logout", key="logout_btn", help="Logout from the dashboard"):
                st.session_state.authenticated = False
                st.session_state.user_id = None
                st.session_state.client_id = None
                st.session_state.client_id_input = ''
                st.session_state.data_fetched = False
                st.rerun()
            
            st.markdown('<hr style="margin: 20px 0;">', unsafe_allow_html=True)
            st.markdown('<div class="section-heading">Filters</div>', unsafe_allow_html=True)
            
            # Auto-fill client ID from login
            default_client_id = st.session_state.get('client_id', '')
            if not st.session_state.client_id_input and default_client_id:
                st.session_state.client_id_input = default_client_id
            
            st.session_state.client_id_input = st.text_input(
                "Client IDs",
                value=st.session_state.client_id_input,
                placeholder="Enter Client IDs (e.g., CLIENT1, CLIENT2)",
                help="Enter one or more Client IDs, separated by commas.",
                autocomplete="off"
            )
            st.markdown('<p style="color: var(--text-color); font-size: 0.85rem; margin-bottom: 1rem;">Use commas to separate multiple IDs.</p>', unsafe_allow_html=True)
            refresh_button = st.button("Refresh Data", help="Fetch data for the specified Client IDs.")

        # Header with fixed main title and User info on the right
        user_info = f' Client ID: {st.session_state.get("client_id_input", "")}'

        right_box = (
            f'<span style="position: absolute; right: 32px; font-size: 0.9rem; font-weight: 500; color: #111827; background: var(--secondary-background-color); border: 1.5px solid var(--border-color); border-radius: 8px; padding: 6px 14px; box-shadow: var(--card-shadow);">{user_info}</span>'
            if st.session_state.get('user_id') else
            '<span style="visibility:hidden; position: absolute; right: 32px;">placeholder</span>'
        )
        date_box = f'<span style="position: absolute; right: 32px; top: 60px; font-size: 0.85rem; font-weight: 400; color: #111827; background: var(--secondary-background-color); border: 1px solid var(--border-color); border-radius: 8px; padding: 4px 12px; box-shadow: var(--card-shadow);">{self.date}</span>' if st.session_state.get('user_id') else ''

        st.markdown(f"""
        <div style="position: relative; width: 100vw; z-index: 3000;">
            <div class="main-title bounce" style="display: flex; align-items: center; justify-content: center; position: relative;">
                <span style="font-size:2.8rem; font-family:'Inter', sans-serif; font-weight:700; color:#059669; letter-spacing:0.02em; flex: 1; text-align: center;">
                    Live Cash Strategy Dashboard
                </span>
                {right_box}
                {date_box} 
            </div>           
        </div>
""", unsafe_allow_html=True)
        


        # Initialize strategy
        # strategy = CashStrategy(mongo_uri=mongo_uri) # This line is removed as per the new_code

        # Data fetching with loading state
        if refresh_button or (st.session_state.client_id_input and not st.session_state.data_fetched):
            with st.spinner("Loading data..."):
                client_ids = [cid.strip() for cid in st.session_state.client_id_input.split(",") if cid.strip()]
                if not client_ids:
                    st.warning("Please enter at least one valid Client ID.")
                    await self.close() # Changed from await strategy.close()
                    return

                filtered_df, stats_df, total_fund, error = await self.fetch_and_process_data(client_ids, None) # Changed from fetch_and_process_data(strategy, client_ids, None)
                if error:
                    st.error(error)
                    await self.close() # Changed from await strategy.close()
                    return

                st.session_state.data_fetched = True
                st.session_state.filtered_df = filtered_df
                st.session_state.stats_df = stats_df
                st.session_state.total_fund = total_fund

        if st.session_state.data_fetched:
            filtered_df = st.session_state.filtered_df
            stats_df = st.session_state.stats_df
            total_fund = st.session_state.total_fund

            # Always update strategy_ids and selectbox based on filtered_df
            strategy_ids = sorted(pd.Series(filtered_df["StrategyID"]).dropna().unique())
            all_options = ["All Strategies"] + strategy_ids
            if (
                "selected_strategy" not in st.session_state or
                st.session_state.selected_strategy not in all_options
            ):
                st.session_state.selected_strategy = "All Strategies"
            with st.sidebar:
                st.markdown('<div class="section-heading">Strategy</div>', unsafe_allow_html=True)
                st.session_state.selected_strategy = st.selectbox(
                    "Select Strategy",
                    options=all_options,
                    index=all_options.index(st.session_state.selected_strategy),
                    help="Select a specific strategy or view all."
                )
            selected_strategy_ids = [st.session_state.selected_strategy] if st.session_state.selected_strategy != "All Strategies" else strategy_ids
            all_strategies_df = filtered_df.copy()  # Save before filtering for the chart
            if st.session_state.selected_strategy != "All Strategies":
                # Get client_ids from session state
                client_ids = [cid.strip() for cid in st.session_state.client_id_input.split(",") if cid.strip()]
                filtered_df, stats_df, total_fund, error = await self.fetch_and_process_data(client_ids, selected_strategy_ids)
                if error:
                    st.error(error)
                    await self.close()
                    return
                st.session_state.filtered_df = filtered_df
                st.session_state.stats_df = stats_df
                st.session_state.total_fund = total_fund

            if "Profit" not in filtered_df.columns:
                st.error(f"Profit column is missing. Available columns: {filtered_df.columns.tolist()}")
                st.dataframe(filtered_df.head())
                await self.close() # Changed from await strategy.close()
                return

            display_columns = [
                "StrategyID", "Symbol", "Pos","Profit & Loss","Yesterday's PNL","BuyPriceAvg","SellPriceAvg","BuyPrice", "SellPrice", "LTP","Close","Qty", "Instrument", "Option", "Strike", "ExpiryDT",
               "ClientID","StopLoss"
            ]
            if "MaxHigh" in filtered_df.columns and any(filtered_df["StrategyID"] == "CST0002"):
                display_columns.insert(display_columns.index("Qty") + 1, "MaxHigh")
            missing_cols = [col for col in display_columns if col not in filtered_df.columns and col != "Profit & Loss"]
            if missing_cols:
                st.error(f"Missing columns: {missing_cols}")
                st.dataframe(filtered_df.head())
                await self.close() # Changed from await strategy.close()
                return

            # KPI Cards
            def get_kpi_value(df, metric, as_int=False):
                series = pd.Series(df[df["Metric"] == metric]["Value"])
                val = series.iloc[0] if not series.empty else "-"
                if as_int and val not in ("-", None, "nan"):
                    try:
                        return str(int(float(str(val).replace(",", "").replace("₹", ""))))
                    except Exception:
                        return val
                return val

            investment = get_kpi_value(stats_df, "Investment", as_int=True)
            overall_profit = get_kpi_value(stats_df, "Overall Profit", as_int=True)
            open_positions = get_kpi_value(stats_df, "Number of Open Positions", as_int=True)
            closed_positions = get_kpi_value(stats_df, "Number of Closed Positions", as_int=True)
            open_closed = f"Open: {open_positions} | Closed: {closed_positions}"

            # Determine color for overall profit
            try:
                overall_profit_value = float(str(overall_profit).replace(",", "").replace("₹", ""))
            except Exception:
                overall_profit_value = 0
            profit_color = "#059669" if overall_profit_value > 0 else ("#DC2626" if overall_profit_value < 0 else "#111827")

            st.markdown(
                '<div class="sticky-kpi-row">' + ''.join([
                self.render_kpi_card("Investment(Overall)", investment, "investment"),
                self.render_kpi_card("Overall Profit", overall_profit, style=f"color:{profit_color};"),
                self.render_kpi_card("Positions", open_closed)
                ]) + '</div>',
                unsafe_allow_html=True
            )
            st.markdown('<div style="height: 80px;"></div>', unsafe_allow_html=True)  # Spacer to prevent overlap
          
            # --- Analytics Section (all statistics as KPI cards) ---
            col1, col2 = st.columns([10.5, 1])
            with col1:
                st.markdown('<div style="text-align:center; margin-top: 0px;" class="section-heading"></div>', unsafe_allow_html=True)
            with col2:
                _refresh_button = st.button("Refresh Data", key="float_refresh", help="Reload data")
            if _refresh_button:
                st.session_state.data_fetched = False
                with st.spinner("Reloading all data..."):
                    client_ids = [cid.strip() for cid in st.session_state.client_id_input.split(",") if cid.strip()]
                    if not client_ids:
                        st.warning("Please enter at least one valid Client ID.")
                        st.rerun()
                        return

                    filtered_df, stats_df, total_fund, error = await self.fetch_and_process_data(client_ids, None)
                    if error:
                        st.error(error)
                        st.rerun()
                        return

                    st.session_state.data_fetched = True
                    st.session_state.filtered_df = filtered_df
                    st.session_state.stats_df = stats_df
                    st.session_state.total_fund = total_fund
                st.rerun()  # Force full page re-render from t

            # st.markdown('<div style="text-align:center; margin-top: 0px;" class="section-heading">Analytics</div>', unsafe_allow_html=True)
            st.markdown("""
            <div style="text-align:center;">
                <div class="section-heading" style="margin-top:0.001rem; margin-bottom:0.5rem;">Analytics</div>
            </div>
            """, unsafe_allow_html=True)

            if stats_df.empty:
                st.warning("No statistics data available for the selected filters.")
            else:
                stats_list = stats_df.to_dict(orient="records")
                kpi_html = '<div class="kpi-row small-kpi-row">'
                for stat in stats_list:
                    label = stat["Metric"]
                    value = stat["Value"]
                    # Exclude Investment, Number of Open Positions, Number of Closed Positions
                    if label in ["Investment", "Number of Open Positions", "Number of Closed Positions"]:
                        continue
                    # Show as integer if possible
                    try:
                        value_int = str(int(float(str(value).replace(",", "").replace("₹", ""))))
                    except Exception:
                        value_int = value
                    kpi_class = ""
                    if label == "Total Profit":
                        kpi_class = "profit"
                    elif label == "Total Loss":
                        kpi_class = "loss"
                    kpi_html += self.render_small_kpi_card(label, value_int, kpi_class)
                kpi_html += '</div>'
                st.markdown(kpi_html, unsafe_allow_html=True)
            st.markdown('<hr style="border:none; border-top:1px solid #e5e7eb; margin:8px 0 8px 0;" />', unsafe_allow_html=True)

            # --- Position Overview Table ---
            st.markdown("""
            <div style="text-align:center;">
                <div class="section-heading" style="margin-top:0.5rem; margin-bottom:0.5rem;">Positions Overview</div>
            </div>
            """, unsafe_allow_html=True)
            filtered_display_df = filtered_df.copy().reset_index(drop=True)
            if "Profit" in filtered_display_df.columns:
                filtered_display_df = filtered_display_df.rename(columns={"Profit": "Profit & Loss"})
            filtered_display_df = filtered_display_df[display_columns]
            def format_list_to_string(lst):
                if isinstance(lst, list):
                    return ", ".join([f"{x:.2f}" if isinstance(x, (int, float)) and not pd.isna(x) else str(x) for x in lst])
                return str(lst) if not pd.isna(lst) else "-"
            filtered_display_df["BuyPrice"] = filtered_display_df["BuyPrice"].apply(format_list_to_string)
            filtered_display_df["SellPrice"] = filtered_display_df["SellPrice"].apply(format_list_to_string)
            filtered_display_df["BuyPriceAvg"] = filtered_display_df["BuyPriceAvg"].apply(format_list_to_string)
            filtered_display_df["SellPriceAvg"] = filtered_display_df["SellPriceAvg"].apply(format_list_to_string)
            filtered_display_df["Strike"] = filtered_display_df["Strike"].apply(lambda x: f"{x:.2f}" if isinstance(x, (int, float)) and not pd.isna(x) else "-")
            filtered_display_df["Close"] = filtered_display_df["Close"].apply(lambda x: f"{x:.2f}" if isinstance(x, (int, float)) and not pd.isna(x) else "-")
            filtered_display_df["Yesterday's PNL"] = filtered_display_df["Yesterday's PNL"].apply(lambda x: f"{x:.2f}" if isinstance(x, (int, float)) and not pd.isna(x) else "-")
            filtered_display_df["ExpiryDT"] = filtered_display_df["ExpiryDT"].astype(str)
            filtered_display_df["StopLoss"] = filtered_display_df["StopLoss"].apply(format_list_to_string)
            filtered_display_df["MaxHigh"] = filtered_display_df["MaxHigh"].apply(format_list_to_string)
   
            # --- Force Pos column sorted as: open → close ---
            # Create a helper column for sorting
            filtered_display_df["PosOrder"] = filtered_display_df["Pos"].map({"open": 0, "close": 1}).fillna(2)

            # Sort dataframe using PosOrder first, then TradeDate (optional secondary sort)
            filtered_display_df = filtered_display_df.sort_values(
                ["PosOrder"], ascending=[True]
            ).reset_index(drop=True)

            # Drop helper column if you don’t want it visible in AgGrid
            filtered_display_df = filtered_display_df.drop(columns=["PosOrder"])

            gb = GridOptionsBuilder.from_dataframe(filtered_display_df)
            gb.configure_default_column(sortable=True, filter=True, resizable=True, groupable=False, minWidth=100, maxWidth=150, cellStyle={"textAlign": "center"}, headerClass="ag-center-cols-header")
            gb.configure_column("StrategyID", minWidth=120, maxWidth=150, cellClass="bounce-on-hover",pinned='left')
            gb.configure_column("ClientID", minWidth=100, maxWidth=120, cellClass="bounce-on-hover",pinned='')
            gb.configure_column("Symbol", minWidth=120, maxWidth=150, cellClass="bounce-on-hover",pinned='left')
            gb.configure_column("Instrument", minWidth=100, maxWidth=120, cellClass="bounce-on-hover")
            gb.configure_column("Option", minWidth=80, maxWidth=100, cellClass="bounce-on-hover")
            gb.configure_column("Strike", minWidth=100, maxWidth=120, cellClass="bounce-on-hover")
            gb.configure_column("ExpiryDT", minWidth=120, maxWidth=150, cellClass="bounce-on-hover")
            gb.configure_column("Pos", minWidth=80, maxWidth=100, cellClass="bounce-on-hover",pinned='left')
            gb.configure_column("Profit & Loss", minWidth=150, maxWidth=120, cellStyle=JsCode("""
                function(params) {
                    if (params.value > 0) {
                        return {'backgroundColor': '#59de90', 'color': '#111827'};
                    } else if (params.value < 0) {
                        return {'backgroundColor': '#f27979', 'color': '#111827'};
                    }
                    return {};
                }
            """), cellClass="bounce-on-hover",pinned='left')
            gb.configure_column("Yesterday's PNL", minWidth=150, maxWidth=180, cellStyle=JsCode("""
        function(params) {
            if (params.value > 0) {
                return {'backgroundColor': '#59de90', 'color': '#111827'};
            } else if (params.value < 0) {
                return {'backgroundColor': '#f27979', 'color': '#111827'};
            }
            return {};
        }
    """), cellClass="bounce-on-hover",pinned='left')
            gb.configure_column("BuyPrice", minWidth=120, maxWidth=150, cellClass="bounce-on-hover")
            gb.configure_column("SellPrice", minWidth=120, maxWidth=150, cellClass="bounce-on-hover")
            gb.configure_column("BuyPriceAvg", minWidth=120, maxWidth=150, cellClass="bounce-on-hover")
            gb.configure_column("SellPriceAvg", minWidth=120, maxWidth=150, cellClass="bounce-on-hover")  
            gb.configure_column("LTP", minWidth=100, maxWidth=120, cellClass="bounce-on-hover")
            gb.configure_column("Close", minWidth=100, maxWidth=120, cellClass="bounce-on-hover")
            gb.configure_column("StopLoss", minWidth=100, maxWidth=120, cellClass="bounce-on-hover")

            gb.configure_column("Qty", minWidth=100, maxWidth=120, cellClass="bounce-on-hover")
            grid_options = gb.build()
            st.markdown(
                '<div style="display: flex; justify-content: center; align-items: flex-start;">',
                unsafe_allow_html=True
            )
            st.markdown("""
<style>
.ag-theme-streamlit .ag-header-cell,
.ag-theme-streamlit .ag-header-group-cell {
    display: flex !important;
    align-items: center !important;
    justify-content: center !important;
}
.ag-theme-streamlit .ag-header-cell-label {
    width: 100%;
    display: flex !important;
    align-items: center !important;
    justify-content: center !important;
}
.ag-theme-streamlit .ag-header-cell-text {
    width: 100%;
    text-align: center !important;
    margin: 0 auto !important;
    display: block !important;
}
.ag-theme-streamlit .ag-header-cell-label span {
    width: 100%;
    text-align: center !important;
    margin: 0 auto !important;
    display: block !important;
}
.ag-theme-streamlit .ag-center-cols-header {
    justify-content: center !important;
    text-align: center !important;
    align-items: center !important;
    display: flex !important;
}
.ag-theme-streamlit .ag-cell {
    text-align: center !important;
    justify-content: center !important;
    font-size: 1.4rem !important;
}
</style>
""", unsafe_allow_html=True)
            AgGrid(
                filtered_display_df,
                gridOptions=grid_options,
                height=400,
                width=950,
                allow_unsafe_jscode=True,
                theme="streamlit",
                update_mode=GridUpdateMode.FILTERING_CHANGED | GridUpdateMode.SORTING_CHANGED,
                data_return_mode=DataReturnMode.FILTERED_AND_SORTED,
                fit_columns_on_grid_load=True,
                custom_css={
                    ".ag-header-cell-label": {"color": "#000000", "fontWeight": "900", "fontSize": "2rem", "textAlign": "center"},
                    ".ag-header-cell-text": {"color": "#000000", "fontWeight": "1000", "fontSize": "2rem", "textAlign": "center"},
                    ".ag-header-group-cell-label": {"color": "#000000", "fontWeight": "900", "fontSize": "2rem", "textAlign": "center"},
                    ".ag-header-group-cell-label span": {"color": "#000000", "fontWeight": "900", "fontSize": "2rem", "textAlign": "center"},
                }
            )
            st.markdown("""
<script>
setTimeout(function() {
    let headers = window.parent.document.querySelectorAll('.ag-theme-streamlit .ag-header-cell-text');
    headers.forEach(function(header) {
        header.style.justifyContent = 'center';
        header.style.textAlign = 'center';
        header.style.width = '100%';
        header.style.display = 'flex';
    });
}, 500);
</script>
""", unsafe_allow_html=True)
            st.markdown('</div>', unsafe_allow_html=True)
            csv = filtered_display_df.to_csv(index=False)
            from datetime import datetime
            now_str = datetime.now().strftime("%Y%m%d_%H%M%S")
            # --- Download CSV and Description in the same row ---
            col1, col2 = st.columns([1, 2])
            with col1:
                st.markdown('<div style="display: flex; align-items: center; height: 100%;">', unsafe_allow_html=True)
                st.download_button(
                    label="Download Positions as CSV",
                    data=csv,
                    file_name=f"positions_data_{now_str}.csv",
                    mime="text/csv",
                    help="Download the positions table as a CSV file."
                )
                st.markdown('</div>', unsafe_allow_html=True)
            with col2:
                st.markdown(
                    '<div class="tab-desc-box" style="margin: 0; height: 100%; display: flex; align-items: center;">View all trading positions with live prices, P&L, and quantities. Use filters and sorting for quick analysis.</div>',
                    unsafe_allow_html=True
                )
            st.markdown('<hr style="border:none; border-top:1px solid #e5e7eb; margin:4px 0 2px 0;" />', unsafe_allow_html=True)

            # --- Profit by Strategy Bar Chart and Pie Chart (side by side with aligned headings) ---
            profit_by_strategy = (
                all_strategies_df.groupby("StrategyID")["Profit"].sum().reset_index()
                .sort_values("Profit", ascending=False)
            )
            bar_colors = profit_by_strategy["Profit"].apply(lambda x: '#059669' if x >= 0 else '#DC2626').tolist()
            if st.session_state.selected_strategy == "All Strategies":
                col1, col2 = st.columns(2)
                with col1:
                    st.markdown('<div style="text-align:center; font-size:1.5rem; font-weight:700; margin-bottom:0.5rem; color:#111111;">Profit by Strategy</div>', unsafe_allow_html=True)
                    fig = self.render_bar_chart(profit_by_strategy, bar_colors)
                    st.plotly_chart(fig, use_container_width=True)
                with col2:
                    st.markdown('<div style="text-align:center; font-size:1.5rem; font-weight:700; margin-bottom:0.5rem; color:#111111;">Profit Distribution by Strategy</div>', unsafe_allow_html=True)
                    custom_palette = ['#059669', '#DC2626', '#111111', '#e5e7eb']  # green, red, black, light gray
                    num_strategies = len(profit_by_strategy["StrategyID"])
                    if num_strategies <= 4:
                        pie_colors = [custom_palette[i % len(custom_palette)] for i in range(num_strategies)]
                    else:
                        palette = plotly.colors.qualitative.Plotly
                        pie_colors = [palette[i % len(palette)] for i in range(num_strategies)]
                    pie_fig = go.Figure(
                        data=[
                            go.Pie(
                                labels=profit_by_strategy["StrategyID"],
                                values=profit_by_strategy["Profit"],
                                hole=0.4,
                                textinfo='label+percent',
                                insidetextorientation='radial',
                                marker=dict(colors=pie_colors)
                            )
                        ]
                    )
                    pie_fig.update_layout(
                        font=dict(family="Inter, sans-serif", color="var(--text-color)", size=14),
                        margin=dict(l=20, r=20, t=20, b=40),
                        showlegend=True
                    )
                    st.plotly_chart(pie_fig, use_container_width=True)
            else:
                st.markdown('<div style="text-align:center; font-size:1.5rem; font-weight:700; margin-bottom:0.5rem; color:#111111;">Profit by Strategy</div>', unsafe_allow_html=True)
                fig = self.render_bar_chart(profit_by_strategy, bar_colors)
                st.plotly_chart(fig, use_container_width=True)
            st.markdown('<hr style="border:none; border-top:1px solid #e5e7eb; margin:8px 0 8px 0;" />', unsafe_allow_html=True)

            # --- Profit & Investment Trends Section ---
            st.markdown('<div class="section-heading" style="text-align:center; font-size:2rem; margin-bottom:0.5rem;">Profit & Investment Trends</div>', unsafe_allow_html=True)
            st.markdown('<div class="performance-controls-row">', unsafe_allow_html=True)
            col1, col2, col3 = st.columns([1,2,1])
            with col1:
                strategy_options = st.session_state.strategy_ids
                default_strategies = [st.session_state.selected_strategy] if (
                    st.session_state.selected_strategy and
                    st.session_state.selected_strategy != "All Strategies" and
                    st.session_state.selected_strategy in strategy_options
                ) else []
                curve_strategies = st.multiselect(
                    "Filter by Strategy",
                    options=strategy_options,
                    default=default_strategies,
                    key="curve_strategies",
                    help="Filter by strategy for the charts."
                )
            with col2:
                min_trade_date = filtered_df["TradeDate"].min()
                max_trade_date = filtered_df["TradeDate"].max()

                if pd.isna(min_trade_date) or pd.isna(max_trade_date):
                    today = datetime.today().date()
                    min_date = today
                    max_date = today
                else:
                    min_date = min_trade_date.date()
                    max_date = max_trade_date.date()

                default_start = min_date
                default_end = max_date

                date_range = st.date_input(
                    "Select Date Range",
                    value=(default_start, default_end),
                    min_value=min_date,
                    max_value=max_date,
                    key="curve_date_range",
                    help="Select date range for performance charts."
                )

            with col3:
                show_markers = st.checkbox("Show Data Points", value=True, help="Toggle markers on line charts.")
            st.markdown('</div>', unsafe_allow_html=True)
            curve_df = filtered_df.copy()
            if curve_strategies:
                curve_df = curve_df[curve_df["StrategyID"].isin(curve_strategies)]
            if len(date_range) == 2:
                start_date, end_date = date_range
                curve_df = curve_df[
                    (curve_df["TradeDate"] >= pd.to_datetime(start_date)) &
                    (curve_df["TradeDate"] <= pd.to_datetime(end_date))
                ]
            curve_df["TradeDate"] = curve_df["TradeDate"].dt.date
            if curve_df.empty:
                st.warning("No data available for the selected strategies and date range.")
            else:
                grouped = curve_df.groupby(["TradeDate"]).agg({
                    "Profit": "sum",
                    "BuyPrice": lambda x: x.iloc[0],
                    "EnQty": lambda x: x.iloc[0],
                }).reset_index()
                def calc_investment(row):
                    buy_prices = row["BuyPrice"] if isinstance(row["BuyPrice"], list) else []
                    en_qtys = row["EnQty"] if isinstance(row["EnQty"], list) else []
                    return sum([p*q for p, q in zip(buy_prices, en_qtys)])
                grouped["Investment"] = grouped.apply(calc_investment, axis=1)
                grouped = grouped.sort_values(["TradeDate"]).reset_index(drop=True)
                grouped["CumulativeProfit"] = grouped["Profit"].cumsum()
                grouped["CumulativeInvestment"] = grouped["Investment"].cumsum()
                grouped["ProfitColor"] = grouped["Profit"].apply(lambda x: 'var(--secondary-color)' if x >= 0 else '#DC2626')
                fig_profit = go.Figure()
                fig_profit.add_trace(
                    go.Scatter(
                        x=grouped["TradeDate"],
                        y=grouped["CumulativeProfit"],
                        mode="lines+markers" if show_markers else "lines",
                        name="Cumulative Profit",
                        marker=dict(size=8, color="#111111"),
                        hovertemplate=(
                            "Date: %{x|%Y-%m-%d}<br>" +
                            "Cumulative Profit: ₹%{y:.2f}<extra></extra>"
                        ),
                        line=dict(width=3, color="#059669")
                    )
                )
                fig_profit.update_layout(
                    title={
                        'text': "<b>Cumulative Profit Over Time</b>",
                        'x': 0.5,
                        'xanchor': 'center',
                        'font': {'size': 22, 'color': '#000000'}
                    },
                    xaxis_title="Trade Date",
                    yaxis_title="Cumulative Profit (INR)",
                    font=dict(family="Inter, sans-serif", color="var(--text-color)", size=14),
                    plot_bgcolor="var(--secondary-background-color)",
                    paper_bgcolor="var(--background-color)",
                    margin=dict(l=40, r=40, t=60, b=60),
                    xaxis=dict(
                        rangeslider=dict(visible=True),
                        type="category",
                        gridcolor="var(--border-color)",
                        tickformat="%Y-%m-%d"
                    ),
                    yaxis=dict(gridcolor="var(--border-color)"),
                    showlegend=False,
                    hovermode="x unified"
                )
                st.plotly_chart(fig_profit, use_container_width=True)
                fig_invest = go.Figure()
                fig_invest.add_trace(
                    go.Scatter(
                        x=grouped["TradeDate"],
                        y=grouped["CumulativeInvestment"],
                        mode="lines+markers" if show_markers else "lines",
                        name="Cumulative Investment",
                        marker=dict(size=8, color="#111111"),
                        hovertemplate=(
                            "Date: %{x|%Y-%m-%d}<br>" +
                            "Cumulative Investment: ₹%{y:.2f}<extra></extra>"
                        ),
                        line=dict(width=3, color="#2563EB")
                    )
                )
                fig_invest.update_layout(
                    title={
                        'text': "<b>Cumulative Investment Over Time</b>",
                        'x': 0.5,
                        'xanchor': 'center',
                        'font': {'size': 22, 'color': '#000000'}
                    },
                    xaxis_title="Trade Date",
                    yaxis_title="Cumulative Investment (INR)",
                    font=dict(family="Inter, sans-serif", color="var(--text-color)", size=14),
                    plot_bgcolor="var(--secondary-background-color)",
                    paper_bgcolor="var(--background-color)",
                    margin=dict(l=40, r=40, t=60, b=60),
                    xaxis=dict(
                        rangeslider=dict(visible=True),
                        type="category",
                        gridcolor="var(--border-color)",
                        tickformat="%Y-%m-%d"
                    ),
                    yaxis=dict(gridcolor="var(--border-color)"),
                    showlegend=False,
                    hovermode="x unified"
                )
                st.plotly_chart(fig_invest, use_container_width=True)
            st.markdown(
                '<div class="tab-desc-box">Track cumulative profit and investment trends over time. Visualize your portfolio’s growth and performance.</div>',
                unsafe_allow_html=True
            )

        else:
            st.markdown(
                "<div style='text-align:center; color:#888; font-size:1.08rem; margin-top:40px; margin-bottom:40px;'>"
                "Please enter Client IDs in the sidebar to begin."
                "</div>",
                unsafe_allow_html=True
            )
            await self.close() # Changed from await strategy.close()

def run_dashboard():
    dashboard = StreamlitDashboard(mongo_uri=mongo_uri)
    asyncio.run(dashboard.run_dashboard_async())

if __name__ == "__main__":
    run_dashboard()





