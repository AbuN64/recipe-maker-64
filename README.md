# recipe-maker-64

Proposes C for N64 decompilation functions and data. [unbake](https://github.com/AbuN64/cake-unbake-64) proves it.

> This is a personal tool. It is not officially maintained and may change or break without notice.

## Install

```sh
pip install git+https://github.com/AbuN64/recipe-maker-64
```

## Usage

```sh
recipe --help
```

It only writes candidate files, which `unbake compare` or `unbake submit` then prove. Design and boundary with unbake are in [docs/usage.md](docs/usage.md).

## Development

Run `bin/dev-setup` once, then `bin/test` and `bin/lint`.

## License

Released under [GNU GPL version 3 or later](LICENSE).
