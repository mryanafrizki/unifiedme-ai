#!/bin/bash

echo "🚀 Setting up Gumloop University Automation..."
echo ""

# Check Python
if ! command -v python3 &> /dev/null; then
    echo "❌ Python 3 is not installed!"
    exit 1
fi

echo "✅ Python 3 found: $(python3 --version)"

# Create virtual environment
if [ ! -d "venv" ]; then
    echo "📦 Creating virtual environment..."
    python3 -m venv venv
fi

# Activate venv
echo "🔧 Activating virtual environment..."
source venv/bin/activate

# Install dependencies
echo "📥 Installing dependencies..."
pip install -r requirements.txt

# Install Playwright browsers
echo "🌐 Installing Playwright Chromium..."
playwright install chromium

# Create config from example
if [ ! -f "config.json" ]; then
    echo "📝 Creating config.json from example..."
    cp config_example.json config.json
    echo ""
    echo "⚠️  IMPORTANT: Edit config.json with your credentials before running!"
fi

# Create screenshots directory
mkdir -p screenshots

echo ""
echo "✅ Setup complete!"
echo ""
echo "Next steps:"
echo "1. Edit config.json with your Gumloop credentials"
echo "2. Add your 6 quiz answers to config.json"
echo "3. Run: source venv/bin/activate && python run_automation.py"
echo ""
