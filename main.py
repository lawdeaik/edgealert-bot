"""
EdgeAlert - Discord Bot for Polymarket Alerts
For beginners: This bot monitors prediction markets and alerts users to profitable opportunities
"""

import discord
from discord.ext import commands, tasks
import requests
import json
import sqlite3
import asyncio
from datetime import datetime, timedelta
import os

# ============================================
# BEGINNER SECTION: Configuration
# ============================================
# You'll set these in Render.com's dashboard (no code changes needed!)
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN', 'your-bot-token-here')
POLYMARKET_REF_CODE = os.getenv('POLYMARKET_REF', 'YOURCODE')
POLL_INTERVAL_MINUTES = int(os.getenv('POLL_INTERVAL', '5'))

# ============================================
# Database Setup (Stores user preferences)
# ============================================
def init_database():
    """Creates tables if they don't exist - runs automatically"""
    conn = sqlite3.connect('edgealert.db')
    c = conn.cursor()
    
    # Users table: stores who's subscribed and their preferences
    c.execute('''CREATE TABLE IF NOT EXISTS users
                 (user_id TEXT PRIMARY KEY,
                  keywords TEXT,
                  threshold REAL DEFAULT 5.0,
                  is_pro INTEGER DEFAULT 0,
                  created_at TEXT)''')
    
    # Alerts cache: prevents duplicate alerts
    c.execute('''CREATE TABLE IF NOT EXISTS alert_cache
                 (market_id TEXT,
                  user_id TEXT,
                  timestamp TEXT,
                  PRIMARY KEY (market_id, user_id))''')
    
    # Market data cache: tracks price changes
    c.execute('''CREATE TABLE IF NOT EXISTS market_cache
                 (market_id TEXT PRIMARY KEY,
                  last_price REAL,
                  last_volume REAL,
                  updated_at TEXT)''')
    
    conn.commit()
    conn.close()

# ============================================
# Polymarket API Functions
# ============================================
def fetch_polymarket_markets():
    """
    Fetches active markets from Polymarket
    Returns: List of market dictionaries
    """
    try:
        # Polymarket's public API endpoint
        url = "https://gamma-api.polymarket.com/markets"
        
        # Request top 100 active markets
        params = {
            'limit': 100,
            'active': 'true',
            'closed': 'false'
        }
        
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        
        markets = response.json()
        
        # Simplify the data structure for easier use
        simplified = []
        for market in markets:
            # Some markets might not have all fields
            try:
                simplified.append({
                    'id': market.get('condition_id', 'unknown'),
                    'question': market.get('question', 'Unknown Market'),
                    'yes_price': float(market.get('outcomePrices', ['0.5'])[0]),  # YES price
                    'volume_24h': float(market.get('volume24hr', 0)),
                    'category': market.get('groupItemTitle', 'general').lower()
                })
            except (KeyError, ValueError, IndexError):
                # Skip markets with incomplete data
                continue
                
        return simplified
        
    except requests.exceptions.RequestException as e:
        print(f"⚠️ API Error: {e}")
        return []  # Return empty list on error, bot will retry next cycle

def calculate_edge(market, user_estimate=0.5):
    """
    Calculates expected value (EV) - simplified for beginners
    
    Args:
        market: Market data dict
        user_estimate: Your probability estimate (default 50%)
    
    Returns:
        edge_percentage: Positive = good bet opportunity
    """
    market_prob = market['yes_price']
    
    # Simple EV formula: (Your odds - Market odds) / Market odds * 100
    edge = ((user_estimate - market_prob) / market_prob) * 100
    
    return round(edge, 1)

# ============================================
# Alert Logic
# ============================================
def check_alert_conditions(market, old_data, threshold):
    """
    Determines if a market qualifies for an alert
    
    Returns: (should_alert: bool, alert_type: str, details: dict)
    """
    # No old data? First time seeing this market
    if not old_data:
        return False, None, {}
    
    old_price = old_data['last_price']
    old_volume = old_data['last_volume']
    new_price = market['yes_price']
    new_volume = market['volume_24h']
    
    # Calculate price change percentage
    price_change = ((new_price - old_price) / old_price) * 100 if old_price > 0 else 0
    
    # Calculate volume spike
    volume_change = ((new_volume - old_volume) / old_volume) * 100 if old_volume > 0 else 0
    
    # Condition 1: Big price move
    if abs(price_change) >= threshold:
        return True, "price_shift", {
            'old_price': old_price * 100,  # Convert to cents
            'new_price': new_price * 100,
            'change_pct': round(price_change, 1),
            'volume_spike': round(volume_change, 1)
        }
    
    # Condition 2: Whale activity (huge volume spike)
    if volume_change > 200 and new_volume > 5000:  # 200% increase + $5K min
        return True, "whale_alert", {
            'old_price': old_price * 100,
            'new_price': new_price * 100,
            'change_pct': round(price_change, 1),
            'volume_spike': round(volume_change, 1)
        }
    
    return False, None, {}

def get_cached_market_data(market_id):
    """Retrieves last known data for a market"""
    conn = sqlite3.connect('edgealert.db')
    c = conn.cursor()
    c.execute('SELECT last_price, last_volume FROM market_cache WHERE market_id = ?', (market_id,))
    result = c.fetchone()
    conn.close()
    
    if result:
        return {'last_price': result[0], 'last_volume': result[1]}
    return None

def update_market_cache(market):
    """Saves current market data for future comparison"""
    conn = sqlite3.connect('edgealert.db')
    c = conn.cursor()
    c.execute('''INSERT OR REPLACE INTO market_cache 
                 (market_id, last_price, last_volume, updated_at)
                 VALUES (?, ?, ?, ?)''',
              (market['id'], market['yes_price'], market['volume_24h'], 
               datetime.now().isoformat()))
    conn.commit()
    conn.close()

def should_send_alert(market_id, user_id):
    """Prevents duplicate alerts (cooldown: 30 minutes)"""
    conn = sqlite3.connect('edgealert.db')
    c = conn.cursor()
    
    # Check if we sent this alert recently
    c.execute('''SELECT timestamp FROM alert_cache 
                 WHERE market_id = ? AND user_id = ?''',
              (market_id, user_id))
    result = c.fetchone()
    
    if result:
        # Parse timestamp
        last_alert_time = datetime.fromisoformat(result[0])
        if datetime.now() - last_alert_time < timedelta(minutes=30):
            conn.close()
            return False  # Too soon, skip
    
    # Mark as alerted
    c.execute('''INSERT OR REPLACE INTO alert_cache 
                 (market_id, user_id, timestamp) VALUES (?, ?, ?)''',
              (market_id, user_id, datetime.now().isoformat()))
    conn.commit()
    conn.close()
    return True

# ============================================
# Discord Bot Setup
# ============================================
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='/', intents=intents)

@bot.event
async def on_ready():
    """Runs when bot starts successfully"""
    print(f'✅ EdgeAlert is online! Logged in as {bot.user}')
    print(f'📊 Monitoring Polymarket every {POLL_INTERVAL_MINUTES} minutes')
    
    # Start the background polling task
    if not poll_markets.is_running():
        poll_markets.start()

# ============================================
# Bot Commands (What users can type)
# ============================================
@bot.command(name='subscribe')
async def subscribe(ctx, *keywords):
    """
    Usage: /subscribe crypto election sports
    Adds keywords to your watchlist
    """
    if not keywords:
        await ctx.send("❌ Please provide keywords! Example: `/subscribe crypto election`")
        return
    
    user_id = str(ctx.author.id)
    keywords_str = ' '.join(keywords).lower()
    
    conn = sqlite3.connect('edgealert.db')
    c = conn.cursor()
    
    # Check if user exists
    c.execute('SELECT keywords FROM users WHERE user_id = ?', (user_id,))
    result = c.fetchone()
    
    if result:
        # Add to existing keywords
        existing = result[0]
        new_keywords = f"{existing} {keywords_str}"
        c.execute('UPDATE users SET keywords = ? WHERE user_id = ?', 
                  (new_keywords, user_id))
    else:
        # New user
        c.execute('''INSERT INTO users (user_id, keywords, created_at)
                     VALUES (?, ?, ?)''',
                  (user_id, keywords_str, datetime.now().isoformat()))
    
    conn.commit()
    conn.close()
    
    # Send confirmation with nice embed
    embed = discord.Embed(
        title="✅ Subscribed!",
        description=f"Now tracking: **{keywords_str}**",
        color=discord.Color.green()
    )
    embed.add_field(name="Threshold", value="5% (change with `/threshold`)")
    embed.add_field(name="Next Steps", value="Sit back! You'll get alerts here.")
    embed.set_footer(text="💎 Upgrade to Pro for unlimited alerts!")
    
    await ctx.send(embed=embed)

@bot.command(name='threshold')
async def set_threshold(ctx, percentage: float):
    """
    Usage: /threshold 10
    Changes your alert sensitivity (1-20%)
    """
    if percentage < 1 or percentage > 20:
        await ctx.send("❌ Threshold must be between 1% and 20%")
        return
    
    user_id = str(ctx.author.id)
    
    conn = sqlite3.connect('edgealert.db')
    c = conn.cursor()
    c.execute('UPDATE users SET threshold = ? WHERE user_id = ?', 
              (percentage, user_id))
    conn.commit()
    conn.close()
    
    await ctx.send(f"✅ Alert threshold set to **{percentage}%**")

@bot.command(name='signup')
async def signup(ctx):
    """Sends Polymarket referral link"""
    embed = discord.Embed(
        title="🎯 Join Polymarket",
        description="Start betting on prediction markets!",
        color=discord.Color.purple()
    )
    embed.add_field(
        name="Exclusive Bonus",
        value=f"Sign up with our link for rewards!",
        inline=False
    )
    embed.add_field(
        name="Link",
        value=f"https://polymarket.com/?ref={POLYMARKET_REF_CODE}",
        inline=False
    )
    embed.set_footer(text="EdgeAlert • Smart Prediction Alerts")
    
    await ctx.send(embed=embed)

@bot.command(name='dashboard')
async def dashboard(ctx):
    """Sends web dashboard link"""
    await ctx.send("📱 **Your Dashboard:** https://your-app-url.com\n(Open to see detailed charts & settings)")

@bot.command(name='stats')
async def stats(ctx):
    """Shows your alert stats"""
    user_id = str(ctx.author.id)
    
    conn = sqlite3.connect('edgealert.db')
    c = conn.cursor()
    
    # Count alerts sent today
    today = datetime.now().date().isoformat()
    c.execute('''SELECT COUNT(*) FROM alert_cache 
                 WHERE user_id = ? AND DATE(timestamp) = ?''',
              (user_id, today))
    alert_count = c.fetchone()[0]
    
    # Get user settings
    c.execute('SELECT keywords, threshold, is_pro FROM users WHERE user_id = ?', 
              (user_id,))
    result = c.fetchone()
    
    conn.close()
    
    if not result:
        await ctx.send("❌ You're not subscribed yet! Use `/subscribe` to start.")
        return
    
    keywords, threshold, is_pro = result
    
    embed = discord.Embed(
        title="📊 Your EdgeAlert Stats",
        color=discord.Color.blue()
    )
    embed.add_field(name="Alerts Today", value=f"{alert_count}/3" if not is_pro else f"{alert_count} (unlimited)", inline=False)
    embed.add_field(name="Watching", value=keywords or "Nothing yet", inline=False)
    embed.add_field(name="Threshold", value=f"{threshold}%", inline=True)
    embed.add_field(name="Plan", value="⭐ PRO" if is_pro else "🆓 Free", inline=True)
    
    await ctx.send(embed=embed)

# ============================================
# Background Task: Market Polling
# ============================================
@tasks.loop(minutes=POLL_INTERVAL_MINUTES)
async def poll_markets():
    """
    Runs every X minutes (default: 5)
    Checks all markets and sends alerts to subscribed users
    """
    print(f"🔄 Polling Polymarket... ({datetime.now().strftime('%H:%M:%S')})")
    
    # Fetch latest market data
    markets = fetch_polymarket_markets()
    
    if not markets:
        print("⚠️ No markets fetched, will retry next cycle")
        return
    
    print(f"📊 Fetched {len(markets)} markets")
    
    # Get all subscribed users
    conn = sqlite3.connect('edgealert.db')
    c = conn.cursor()
    c.execute('SELECT user_id, keywords, threshold, is_pro FROM users')
    users = c.fetchall()
    conn.close()
    
    if not users:
        print("📭 No subscribed users yet")
        return
    
    alerts_sent = 0
    
    # Check each market against each user's preferences
    for market in markets:
        # Get historical data for this market
        old_data = get_cached_market_data(market['id'])
        
        # Check each subscribed user
        for user_id, keywords, threshold, is_pro in users:
            # Match keywords (e.g., user watches "crypto", market is "crypto")
            user_keywords = keywords.split() if keywords else []
            
            # Skip if market doesn't match user's interests
            if user_keywords and not any(kw in market['question'].lower() or kw == market['category'] 
                                        for kw in user_keywords):
                continue
            
            # Check if this market should trigger an alert
            should_alert, alert_type, details = check_alert_conditions(
                market, old_data, threshold
            )
            
            if should_alert:
                # Check cooldown (prevent spam)
                if not should_send_alert(market['id'], user_id):
                    continue
                
                # Check free tier limits (3 alerts/day)
                if not is_pro:
                    today = datetime.now().date().isoformat()
                    conn = sqlite3.connect('edgealert.db')
                    c = conn.cursor()
                    c.execute('''SELECT COUNT(*) FROM alert_cache 
                                 WHERE user_id = ? AND DATE(timestamp) = ?''',
                              (user_id, today))
                    alert_count = c.fetchone()[0]
                    conn.close()
                    
                    if alert_count >= 3:
                        continue  # Free tier limit reached
                
                # Send the alert!
                try:
                    user = await bot.fetch_user(int(user_id))
                    await send_alert_embed(user, market, alert_type, details)
                    alerts_sent += 1
                except Exception as e:
                    print(f"⚠️ Failed to send alert to {user_id}: {e}")
        
        # Update cache with new data
        update_market_cache(market)
    
    print(f"✉️ Sent {alerts_sent} alerts this cycle")

async def send_alert_embed(user, market, alert_type, details):
    """
    Creates and sends a beautiful alert embed to the user
    """
    # Calculate edge
    edge = calculate_edge(market)
    
    # Determine color based on price movement
    if details['change_pct'] > 0:
        color = discord.Color.green()
        emoji = "📈"
    else:
        color = discord.Color.red()
        emoji = "📉"
    
    # Special emoji for whale alerts
    if alert_type == "whale_alert":
        emoji = "🐋"
        color = discord.Color.orange()
    
    # Create embed
    embed = discord.Embed(
        title=f"{emoji} MAJOR MOVE DETECTED",
        description=f"**{market['question']}**",
        color=color,
        timestamp=datetime.now()
    )
    
    # Add market details
    embed.add_field(
        name="💰 Price Movement",
        value=f"{details['old_price']:.0f}¢ → {details['new_price']:.0f}¢ ({details['change_pct']:+.1f}%)",
        inline=True
    )
    
    embed.add_field(
        name="📊 Volume Spike",
        value=f"+{details['volume_spike']:.0f}%" if details['volume_spike'] > 0 else "Normal",
        inline=True
    )
    
    embed.add_field(
        name="📈 Your Edge",
        value=f"{edge:+.1f}% EV" if abs(edge) > 1 else "Neutral",
        inline=True
    )
    
    # Add potential profit estimate
    if abs(edge) > 5:
        potential_profit = abs(edge) * 10  # $100 bet example
        embed.add_field(
            name="💎 Potential",
            value=f"${potential_profit:.0f} on $100 bet",
            inline=False
        )
    
    # Category tag
    embed.add_field(
        name="🏷️ Category",
        value=market['category'].capitalize(),
        inline=True
    )
    
    # Whale warning
    if alert_type == "whale_alert":
        embed.add_field(
            name="🐋 Whale Activity",
            value="⚠️ Large trader(s) active!",
            inline=True
        )
    
    embed.set_footer(text="⚡ Powered by EdgeAlert • React 🎯 to trade")
    
    # Send with buttons (view buttons are simulated with text since Discord.py buttons need views)
    try:
        message = await user.send(embed=embed)
        
        # Add reaction emojis for quick actions
        await message.add_reaction("🎯")  # Trade
        await message.add_reaction("📊")  # Details
        await message.add_reaction("🔕")  # Mute
        
    except discord.Forbidden:
        print(f"⚠️ Cannot DM user {user.id} (DMs disabled)")

# ============================================
# Error Handling
# ============================================
@bot.event
async def on_command_error(ctx, error):
    """Handles command errors gracefully"""
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"❌ Missing argument! Check `/help {ctx.command}` for usage.")
    elif isinstance(error, commands.BadArgument):
        await ctx.send(f"❌ Invalid argument! Example: `/threshold 10`")
    else:
        await ctx.send(f"❌ Something went wrong! Try again or contact support.")
        print(f"Error: {error}")

# ============================================
# Startup
# ============================================
if __name__ == "__main__":
    print("🚀 Starting EdgeAlert Bot...")
    print("=" * 50)
    
    # Initialize database
    init_database()
    print("✅ Database initialized")
    
    # Check configuration
    if DISCORD_TOKEN == 'your-bot-token-here':
        print("❌ ERROR: Set your DISCORD_TOKEN in environment variables!")
        print("   Go to Render.com dashboard → Environment → Add DISCORD_TOKEN")
        exit(1)
    
    print(f"✅ Polymarket referral: {POLYMARKET_REF_CODE}")
    print(f"✅ Poll interval: {POLL_INTERVAL_MINUTES} minutes")
    print("=" * 50)
    
    # Start bot
    try:
        bot.run(DISCORD_TOKEN)
    except discord.LoginFailure:
        print("❌ Invalid bot token! Check your DISCORD_TOKEN environment variable.")
    except Exception as e:
        print(f"❌ Fatal error: {e}")
