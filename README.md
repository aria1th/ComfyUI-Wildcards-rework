# ComfyUI_Randomizer

A powerful ComfyUI custom node for **dynamic prompt generation** using wildcards and bracket expressions. Create infinite variations from template prompts with weighted selection, multi-picks, and nested expansions.

---

## ✨ Features

| Feature | Syntax | Example |
|---------|--------|---------|
| **Wildcard Expansion** | `__name__` | `a __colors__ dress` → `a red dress` |
| **Random Selection** | `{a\|b\|c}` | `{red\|blue\|green}` → `blue` |
| **Weighted Choice** | `option$weight` | `{rare$1\|common$10}` → `common` (10× more likely) |
| **Multi-Pick** | `{N$$a\|b\|c}` | `{2$$red\|blue\|green\|yellow}` → `red, yellow` |
| **Range Pick** | `{N-M$$a\|b\|c}` | `{1-3$$a\|b\|c\|d}` → `b, d` |
| **Custom Separator** | `{N$$sep$$...}` | `{2$$ and $$a\|b\|c}` → `a and c` |
| **Nested Expansion** | `{a\|{b\|c}}` | Full recursive support |

---

## 🔧 Installation

```bash
cd ComfyUI/custom_nodes
git clone <repository-url> ComfyUI_Randomizer
# Restart ComfyUI - dependencies install automatically
```

**Dependencies**: `chardet` (auto-installed)

---

## 📦 Node: Text Wildcards

**Category**: `Randomizer`

### Inputs

| Input | Type | Description |
|-------|------|-------------|
| `text` | STRING | Prompt template with wildcards/brackets |
| `seed` | INT | Random seed for reproducibility |
| `refresh` | INT | Set to `1` to reload wildcard files |

### Outputs

| Output | Type | Description |
|--------|------|-------------|
| `text` | STRING | Expanded prompt |
| `ascii_text` | STRING | ASCII-safe version (non-ASCII replaced) |

---

## 📂 Wildcards Directory

Place `.txt` files in the `wildcards/` folder. Each line becomes a selectable option.

### Example: `wildcards/colors.txt`

```text
red
blue
green
yellow$3    # 3× more likely to be selected
```

### Usage in Prompt

```text
a beautiful __colors__ sunset over __places__
```

**Expands to**: `a beautiful yellow sunset over mountain lake`

---

## 📖 Syntax Reference

### Basic Selection

```text
# Random from wildcard file
__filename__

# Random from inline options  
{option1|option2|option3}
```

### Weighted Selection

```text
# Weight with $N suffix (higher = more likely)
{rare_item$1|common_item$5|very_common$20}
```

### Multi-Pick

```text
# Pick exactly 2 items
{2$$red|blue|green|yellow}
→ "red, green"

# Pick 1-3 items randomly  
{1-3$$apple|banana|cherry|date}
→ "banana, date"

# Custom separator
{2$$ and $$cats|dogs|birds}
→ "cats and birds"
```

### Nested Expressions

```text
# Wildcards can reference other wildcards
# In colors.txt:
{basic|__special_colors__}

# Bracket groups can nest
{{red|blue}|{green|yellow}}
```

---

## 🎨 Advanced Tips

1. **Organize wildcards in subfolders** - Use `__folder/filename__` syntax
2. **Chain expansions** - `__style__ portrait of __character__ wearing __outfit__`
3. **Combine with ControlNet** - Vary prompts while maintaining pose/composition
4. **Batch generation** - Same seed = same expansion for A/B testing

---

## 📄 License

MIT License
