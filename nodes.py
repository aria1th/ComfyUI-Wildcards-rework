"""
ComfyUI_Randomizer - Prompt randomization nodes for ComfyUI

Provides:
- TextWildcards: Wildcard expansion and random selection
"""

from .wildcards import TextWildcards

NODE_CLASS_MAPPINGS = {
    "TextWildcards": TextWildcards,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "TextWildcards": "Text Wildcards",
}