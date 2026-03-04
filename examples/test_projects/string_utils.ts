/**
 * String utility functions — TypeScript test file for mole v3.
 * Tests: hole discovery, type extraction via tsc, fill, verify.
 */

// @mole:behavior convert a string to title case (capitalize first letter of each word)
// @mole:ensures handles empty string, multiple spaces, mixed case input
function titleCase(input: string): string {
    const result: string = hole("convert input to title case");
    return result;
}

interface WordFrequency {
    word: string;
    count: number;
}

// @mole:behavior count word frequencies in a string, return sorted by count descending
// @mole:requires input is non-null
// @mole:ensures each word lowercased, sorted by count desc then alphabetical
function wordFrequencies(text: string): WordFrequency[] {
    const frequencies: WordFrequency[] = hole("count word frequencies and sort");
    return frequencies;
}

// @mole:behavior truncate string to maxLen chars, add ellipsis if truncated
// @mole:ensures never returns string longer than maxLen + 3 (for "...")
// @mole:ensures returns original string unchanged if shorter than maxLen
function truncate(text: string, maxLen: number): string {
    const result: string = hole("truncate with ellipsis");
    return result;
}

// Declare hole as a placeholder function (will be replaced by mole)
declare function hole(description: string): any;
