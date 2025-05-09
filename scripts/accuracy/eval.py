#!/usr/bin/env python3
from typing import List, Dict, Tuple, Callable, Union, Literal
from dataclasses import dataclass
from pprint import pprint
from pathlib import Path
import subprocess
import argparse
import logging
import csv
import sys
import re
from tabulate import tabulate

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

desc="""This script evaluates .hfst/.hfstol coverage and accuracy on csv-like corpus. By default it reads corpus from stdin.

The input corpus lines must contain a single token and have format 'hfst_in,hfst_out'. The script will stream `hfst_in` column into provided --hfst-analyzer and evaluate accuracy.
Line is considered to pass accuracy score if AT LEAST ONE of --hfst-analyzer outputs matches `hfst_out` value.

In code's 'Custom accuracy functions' block you can add your own accuracy functions.
Function must have typing `(str, str) -> bool`, where strings are like 'car<n>><pl>','car<n>><sg>'.
Don't forget to add your function to global `accuracy_funcs` dict.
"""

parser = argparse.ArgumentParser(description=desc,
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog="Author: Kartina Elen @ HSE University 2025")
parser_required = parser.add_argument_group('required')
parser_required.add_argument('-H', '--hfst-analyzer', required=True,
    help='Hfst analyzer file that turns \'words\' into \'word<n>><pl>\'')
parser_optional = parser.add_argument_group('optional')
parser_optional.add_argument('-i', '--csv', default='STDIN',
    help='Csv file with pairs like \'words,word<n>><pl>\'. Set to \'STDIN\' to read csv from stdin. Default: \'STDIN\'')
parser_optional.add_argument('-f', '--output-format', default='table',
                             choices=['table', 'json', 'json_indent'],
    help='Metrics print format. \'table\'=pretty human-readable. Default is \'table\'')
parser_optional.add_argument('--drop-first-csv-row', action='store_true',
                             help='Ignore the first line from the --csv input. Useful for csv\'s with column names.')
parser_optional.add_argument('--hfst-translit',
    help='Hfst transliterator to be applied to --hfst-analyzer\'s output stems \'слово<n>\' -> \'slovo<n>\' if needed')
parser_optional.add_argument('--details-dir',
    help='Csv directory where details should be logged in format \'wordform,tagged,status\'')

tag_aliases = {
    '<lat>': '<dat>',
    '<o>': '<obl>'
}

AccFuncsMapping = Dict[str, Callable[[str, str], bool]]

###############
#    INPUT    #
###############
def read_csv(file: Union[str, Path], drop_first = False) -> Tuple[List[str], List[str]]:
    if isinstance(file, str):
        file = Path(file)
    if not file.exists():
        raise FileNotFoundError(file)
    wordforms = []
    tagged = []
    with open(file, 'r', encoding='utf-8') as f:
        reader = csv.reader(f)
        if drop_first:
            next(reader) # col names
        for w, t in reader:
            wordforms.append(w)
            tagged.append(t)
    assert len(wordforms) == len(tagged)
    return wordforms, tagged

def read_stdin(drop_first = False) -> Tuple[List[str], List[str]]:
    wordforms = []
    tagged = []
    reader = csv.reader(sys.stdin)
    if drop_first:
        next(reader) # col names
    for w, t in reader:
        wordforms.append(w)
        tagged.append(t)
    assert len(wordforms) == len(tagged)
    return wordforms, tagged

###############
#    HFST     #
###############
class HfstException(Exception):
    pass

@dataclass
class ParsedItem:
    input_str: str
    out_variants: List[str]

    def variants(self) -> str:
        return f'{"/".join(self.out_variants)}'
    def __str__(self):
        return f'{self.input_str} -> {self.variants()}'
    def __repr__(self):
        return f'[ParsedItem {str(self)}]'

def parse_apertium(stdout: str) -> List[ParsedItem]:
    items: List[ParsedItem] = []
    # regex: all strings like '^+$' (apertium format) with no nested ^ or $ 
    for raw in re.finditer(r'\^([^\^\$]+)\$', stdout):
        apertium = raw.groups()[-1]
        input_str, *output_variants = apertium.split('/')
        items.append(ParsedItem(input_str=input_str,
                                out_variants=output_variants))
    return items

def hfst_lookup(hfst_file: Union[Path, str],
                input_strings: List[str]) -> List[ParsedItem]:
    if isinstance(hfst_file, str):
        hfst_file = Path(hfst_file)
    if not hfst_file.is_file():
        raise FileNotFoundError(hfst_file)
    proc = subprocess.Popen(['hfst-lookup', '-q', '--output-format', 'apertium', hfst_file],
                            stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT)
    stdout, stderr = proc.communicate(input=bytes('\n'.join(input_strings), 
                                      encoding='utf-8'))
    stdout = stdout.decode() if stdout else ''
    stderr = stderr.decode() if stderr else ''
    if proc.returncode != 0:
        raise HfstException(f'hfst-lookup: stdout={stdout}; stderr={stderr}')
    return parse_apertium(stdout)

##############################
#    Stem Transliteration    #
##############################
def get_stem(tagged: str) -> str:
    return re.findall(r'>?([^<>]+)<', tagged)[0]

def make_latin(items: List[ParsedItem], hfst: str):
    cyr2lat: Dict[str, List[str]] = {}
    # gathering all present stems
    for it in items:
        if '*' in it.out_variants[0]:
            continue
        for var in it.out_variants:
            stem = get_stem(var)
            cyr2lat[stem] = []
    # transliterating all stems at once is ~40 times faster than transliterating one stem at a time
    for translit in hfst_lookup(hfst, list(cyr2lat.keys())):
        cyr2lat[translit.input_str] = translit.out_variants
    # replacing cyr stems with lat stems
    for it in items:
        if '*' in it.out_variants[0]:
            continue
        new_variants: List[str] = []
        for var in it.out_variants:
            stem = get_stem(var)
            for lat_stem in cyr2lat[stem]:
                new_variants.append(var.replace(stem, lat_stem))
        it.out_variants = new_variants

###################################
#    Custom accuracy functions    #
###################################
def match_exact(tagged1: str, tagged2: str) -> bool:
    return tagged1 == tagged2

def match_stem(tagged1: str, tagged2: str) -> bool:
    stem1 = get_stem(tagged1)
    stem2 = get_stem(tagged2)
    return stem1 == stem2

def match_pos(tagged1: str, tagged2: str) -> bool:
    pos_pattern = r'[^<>\n]+<([^<>]+)>'
    pos1 = re.findall(pos_pattern, tagged1)[0]
    pos2 = re.findall(pos_pattern, tagged2)[0]
    return pos1 == pos2

def match_stem_and_pos(tagged1: str, tagged2: str) -> bool:
    return match_stem(tagged1, tagged2) and match_pos(tagged1, tagged2)

def match_unordered(tagged1: str, tagged2: str) -> bool:
    tags1 = set(re.findall(r'<[^<>]+>', tagged1))
    tags2 = set(re.findall(r'<[^<>]+>', tagged2))
    return match_stem(tagged1, tagged2) and tags1 == tags2

accuracy_funcs: AccFuncsMapping = {
    'exact_match':          match_exact,
    'stem_match':           match_stem,
    'pos_match':            match_pos,
    'stem_and_pos_match':   match_stem_and_pos,
    'unordered_tags_match': match_unordered,
}
################
#    OUTPUT    #
################
def log_details(dir: Path, out_file: str, wordform: str, reference: str, real_output: str, result: str):
    if dir is None:
        return
    if not out_file.endswith('.csv'):
        out_file += '.csv'
    with open(dir.joinpath(out_file), 'a', encoding='utf-8') as out_f:
        writer = csv.writer(out_f)
        writer.writerow((wordform, reference, real_output, result))

def table_results(results: dict, format: Literal['table', 'json', 'json_indent']):
    if format == 'table':
        table = [['coverage', results['recognized']/results['total'], f"{results['recognized']}/{results['total']}"]]
        for metric, data in results['accuracy'].items():
            table.append([metric, data['acc'], f"{data['correct']}/{data['recognized']}"])
        for row in table:
            row[1] = f'{row[1] * 100:.2f}%'
        print(tabulate([['metric', 'value', 'absolute'], *table],
                        tablefmt="rounded_outline", headers='firstrow'))
        print()
    else:
        if format == 'json':
            print(results)
        if format == 'json_indent':
            pprint(results)

####################
#    Evaluation    #
####################
def replace_aliases(tagged: List[str]):
    for i in range(len(tagged)):
        for old, new in tag_aliases.items():
            tagged[i] = tagged[i].replace(old, new)

def compare(reference: List[str], predicted: List[ParsedItem], 
            acc_funcs: AccFuncsMapping, details_dir: Path) -> dict:
    if len(reference) != len(predicted):
        raise RuntimeError(f'Reference count {len(reference)} != Predicted count {len(predicted)}')
    total = len(reference)
    recognized = 0
    acc_results = {k: {'correct': 0, 'recognized': 0} 
                   for k in acc_funcs.keys()}
    # evaluating absolute counts
    for ref, pred in zip(reference, predicted):
        is_correct = False
        if '*' in pred.out_variants[0]:
            log_details(details_dir, 'unknown', pred.input_str, ref, pred.variants(), 'UNKNOWN')
            continue
        recognized += 1
        for acc_type in acc_funcs.keys():
            acc_results[acc_type]['recognized'] += 1
            for pred_var in pred.out_variants:
                if acc_funcs[acc_type](ref, pred_var):
                    is_correct = True
                    acc_results[acc_type]['correct'] += 1
                    log_details(details_dir, acc_type, pred.input_str, ref, pred.variants(), 'CORRECT')
                    break
            if not is_correct:
                log_details(details_dir, acc_type, pred.input_str, ref, pred.variants(), 'FAIL')
    # evaluating fractional counts
    for acc_type in acc_funcs.keys():
        if acc_results[acc_type]['recognized'] == 0:
            acc_results[acc_type]['acc'] = 0
        else:
            acc_results[acc_type]['acc'] = acc_results[acc_type]['correct'] /\
                                           acc_results[acc_type]['recognized']
    return {
        'total': total,
        'recognized': recognized,
        'accuracy': acc_results
    }

def main():
    args = parser.parse_args()
    details_dir = None
    if args.details_dir:
        details_dir = Path(args.details_dir)
        details_dir.mkdir(exist_ok=True, parents=True)
        for f in details_dir.iterdir():
            f.unlink()

    # READING THE DATA
    if args.csv == 'STDIN':
        wordforms, reference = read_stdin(drop_first=args.drop_first_csv_row)
    else:
        wordforms, reference = read_csv(args.csv, drop_first=args.drop_first_csv_row)
    # PREPROCESS
    replace_aliases(reference)
    predicted = hfst_lookup(args.hfst_analyzer, wordforms)
    if args.hfst_translit:
        make_latin(predicted, hfst=args.hfst_translit)
    # EVALUATE
    results = compare(reference, predicted, accuracy_funcs, details_dir=details_dir)
    # PRINT
    table_results(results, format=args.output_format)

if __name__ == '__main__':
    main()
    #print(parse_apertium(call_hfst_lookup(HFST_ANALYZER, ['wi-rd-i', 'wi-rd-i', 'wi-rd-i'])))
