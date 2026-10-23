# recipe-maker-64

Cleanup patches and names for N64 decompilation trees: module structure, names, original types, style. [unbake](https://github.com/AbuN64/cake-unbake-64) builds and proves the tree.

> This is a personal tool. It is not officially maintained and may change or break without notice.

## Install

```sh
pip install git+https://github.com/AbuN64/recipe-maker-64
```

## Usage

```sh
recipe --help
```

It writes patches under `.unbake/recipe/patches/`; `recipe apply` applies them and the tree is proved by building. Design and boundary with unbake are in [docs/usage.md](docs/usage.md).

## Development

Run `bin/dev-setup` once, then `bin/test` and `bin/lint`.

## License

Released under [GNU GPL version 3 or later](LICENSE).
